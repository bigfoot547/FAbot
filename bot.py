import asyncio

import e6handler
import irc
import json
import time
import re
import collections

import fahandler

VALID_MD5 = re.compile('[\\da-f]{32}', re.IGNORECASE)


class FABot(irc.SASLIRCBot):
    def __init__(self, config, secrets):
        super().__init__(secrets['sasluser'], secrets['saslpass'], require_auth=config['require_auth'], nick=config['nick'], ident=config['ident'], realname=config['realname'])
        self.__secrets = secrets
        self.logchan = config['logchan']
        self._load_bot_data('bot.json')
        self.onchans = []
        self._last_try_join_time = time.time()
        self._last_e621_api_call = 0
        self.last_e6_md5_links = {}

        self.fa_recent_lookups = collections.deque(maxlen=20)
        self.e6_recent_post_lookups = collections.deque(maxlen=20)
        self.e6_recent_md5_lookups = collections.deque(maxlen=20)

    def _load_bot_data(self, filename):
        self.data_filename = filename
        try:
            with open(filename, 'r') as fp:
                self.data = json.load(fp)
        except IOError:
            self.data = {'channels': {}, 'admins': [], 'optout': []}
            self._save_bot_data()

    def _save_bot_data(self):
        with open(self.data_filename, 'w') as fp:
            json.dump(self.data, fp)

    async def send_message(self, target, message):
        await self.write_line(irc.IRCLine(verb='PRIVMSG', params=[target, message]))

    async def send_notice(self, target, message):
        await self.write_line(irc.IRCLine(verb='NOTICE', params=[target, message]))

    async def send_log(self, note, message):
        await self.send_message(self.logchan, f'\2{note.upper()}\2: {message}')

    async def on_register(self):
        await super().on_register()
        await self.write_line(irc.IRCLine(verb='MODE', params=[self.nick, '+Qu-iw']))
        await self.send_log('bot', f'Successfully connected and registered with account {self.account}')

        for channame in self.data['channels']:
            channel = self.data['channels'][channame]
            if 'key' in channel:
                await self.join(channame, channel['key'])
            else:
                await self.join(channame)

    async def handle_verb_join(self, line):
        channel = line.params[0].lower()
        if line.source['nick'] == self.nick:
            if channel not in self.onchans:
                self.onchans.append(channel)

    async def handle_verb_part(self, line):
        channel = line.params[0].lower()
        if line.source['nick'] == self.nick:
            if channel in self.onchans:
                self.onchans.remove(channel)

    async def handle_verb_kick(self, line):
        channel = line.params[0].lower()
        kicked = line.params[1]
        if channel in self.onchans and kicked == self.nick:
            self.onchans.remove(channel)

    async def tick_client(self):
        await super().tick_client()

        now = time.time()
        if now - self._last_try_join_time > 10:
            self._last_try_join_time = now
            for channame in self.data['channels']:
                if channame not in self.onchans:
                    channel = self.data['channels'][channame]
                    if 'key' in channel:
                        await self.join(channame, channel['key'])
                    else:
                        await self.join(channame)

    async def handle_verb_privmsg(self, line):
        target = line.params[0]
        message = line.params[1]

        if message.startswith('\1') and message.endswith('\1'):
            ctcpmsg = message[1:-1]
            cmd = ctcpmsg.split(' ')[0].upper()
            if cmd == 'VERSION':
                await self.send_notice(line.source['nick'], '\1VERSION FAbot v1 by \\\1')
            return

        if target == self.nick:  # PM command
            splt = message.split(' ')
            try:
                await asyncio.wait_for(self.handle_pm_command(line, splt[0].lower(), splt[1:]), 5.0)
            except asyncio.TimeoutError:
                await self.send_notice(line.source['nick'], "The command could not be completed in time.")
        elif target.lower() in self.data['channels']:
            if message == '': return  # Not traditionally possible, but allowed by the protocol
            targetchan = target.lower()

            prefixes = ''
            if 'prefix' in self.data['channels'][targetchan]:
                prefixes = self.data['channels'][targetchan]['prefix']

            if message[0] in prefixes:
                stripmsgsplt = message[1:].split(' ')
                await self.handle_channel_command(line, stripmsgsplt[0].lower(), stripmsgsplt[1:])
            elif re.match(f'^{re.escape(self.nick)}[:,]? ', message, re.IGNORECASE):
                splt = message.split(' ')
                if len(splt) < 2: return
                await self.handle_channel_command(line, splt[1], splt[2:])
            else:
                opted_out = 'account' in line.tags and line.tags['account']['value'].lower() in self.data['optout']

                await self.handle_e621_static1(message, line, target, opted_out)
                if not opted_out:
                    await self.handle_furaffinity(message, line, target)
                    await self.handle_e621_posts(message, line, target)

    async def handle_furaffinity(self, message, line, target):
        famatches = list(dict.fromkeys(re.findall(fahandler.FURAFFINITY_POST_PATTERN, message)))
        now = time.time()
        for match in famatches:
            try:
                info = None
                for lookup in self.fa_recent_lookups:
                    if lookup[0] == match and now - lookup[1] < 300:
                        await self.send_log('FA',
                                            f"Found cached post \2{match}\2 (requested by {line.sourceraw} in {target})")
                        info = lookup[2]

                if info is None:
                    await self.send_log('FA',
                                        f"Looking up post \2{match}\2 (requested by {line.sourceraw} in {target})")
                    info = fahandler.get_info(self.__secrets['auth']['furaffinity'], match)
                    self.fa_recent_lookups.append((match, now, info))

                if 'error' in info:
                    await self.send_log('FA', f"Lookup failed for \2{match}\2: Error: {info['error']}")
                    await self.send_message(target, f"[FA/{match}] Error: {info['error']}")
                    continue

                title: str
                download: str

                if 'title' in info:
                    title = f"'{info['title']}' by {info['artist']}"
                else:
                    title = f"(unknown) by {info['artist']}"

                if 'cdn-link' in info:
                    download = "Image URL: " + info['cdn-link']
                elif 'download-link' in info:
                    download = "Download URL: " + info['download-link']
                else:
                    download = "No download link found"
                infostr = f"{title} | Rating: {info['rating']} | {download}"

                await self.send_message(target, f"[FA/{match}] {infostr}")
                await self.send_log('FA', f"Lookup succeeded for \2{match}\2: {infostr}")
            except Exception as ex:
                await self.send_log('FA', f"Lookup failed for \2{match}\2: Exception raised: {type(ex).__name__}: {str(ex)}")
                await self.send_message(target, f"[FA/{match}] Error: An exception occurred while parsing the webpage.")

    def e621_create_poststr(self, post, include_post=False):
        artists: str
        artist_tags = post['tags']['artist']
        if len(artist_tags) == 1:
            artists = artist_tags[0]
        elif len(artist_tags) == 0:
            artists = "(unknown)"
        elif len(artist_tags) <= 3:
            artists = ', '.join(artist_tags)
        else:
            artists = f"{', '.join(artist_tags[:3])} (and {len(artist_tags) - 3} more)"

        poststr = f"Art by {artists} | Rating: {e6handler.get_rating(post['rating'])} | Score: {post['score']['total']:+} | "
        if post['flags']['deleted']:
            poststr += "Post is deleted | "
        elif post['flags']['flagged']:
            poststr += "Post is flagged for deletion | "

        if include_post:
            poststr += f"Post: https://e621.net/posts/{post['id']} | "

        if 'file' in post:
            file_obj = post['file'] or {'width': None, 'height': None, 'url': None}
            poststr += f"Image ({file_obj['width'] or '?'}x{file_obj['height'] or '?'}): {file_obj['url'] or '(unknown)'}"
        else:
            poststr += f"Image (?x?): (unknown)"

        return poststr

    async def handle_e621_posts(self, message, line, target):
        e6matches = list(dict.fromkeys(re.findall(e6handler.E621_POST_PATTERN, message)))
        now = time.time()
        for match in e6matches:
            try:
                post = None
                for lookup in self.e6_recent_post_lookups:
                    if lookup[0] == match and now - lookup[1] < 300:
                        await self.send_log('E621', f"Found cached post \2{match}\2 (requested by {line.sourceraw} in {target})")
                        post = lookup[2]

                if post is None:
                    await self.send_log('E621', f"Looking up post \2{match}\2 (requested by {line.sourceraw} in {target})")
                    since_last = now - self._last_e621_api_call
                    if since_last < 0.6:
                        await asyncio.sleep(0.6 - since_last)
                        now = time.time()
                    post = e6handler.get_post_info(self.__secrets['auth']['e621'], match)
                    self._last_e621_api_call = now
                    self.e6_recent_post_lookups.append((match, now, post))

                if 'error' in post:
                    await self.send_log('E621', f"Lookup failed for \2{match}\2: Error: {post['error']}")
                    await self.send_message(target, f"[E621/{match}] Error: {post['error']}")
                    continue

                poststr = self.e621_create_poststr(post)
                await self.send_message(target, f"[E621/{match}] {poststr}")
                await self.send_log('E621', f"Lookup succeeded for \2{match}\2: {poststr}")
            except Exception as ex:
                await self.send_log('E621', f"Lookup failed for \2{match}\2: Exception raised: {type(ex).__name__}: {str(ex)}")
                await self.send_message(target, f"[E621/{match}] Error: An exception occurred while querying post info.")

    async def e621_search_md5(self, md5_hash, source, target):
        now = time.time()
        results = None
        for lookup in self.e6_recent_md5_lookups:
            if lookup[0] == md5_hash and now - lookup[1] < 300:
                await self.send_log('E621', f"Found cached post \2{md5_hash}\2 (requested by {source} in {target})")
                results = lookup[2]
                break

        if results is None:
            await self.send_log('E621', f"Searching for post \2{md5_hash}\2 (requested by {source} in {target})")
            since_last = now - self._last_e621_api_call
            if since_last < 0.6:
                await asyncio.sleep(0.6 - since_last)
                now = time.time()

            results = e6handler.search_post_hash(self.__secrets['auth']['e621'], md5_hash)
            self.e6_recent_md5_lookups.append((md5_hash, now, results))
        return results

    async def handle_e621_static1(self, message, line, target, opted_out):
        e6matches = list(dict.fromkeys(re.findall(e6handler.E621_IMAGE_PATTERN, message)))
        targetchan = target.lower()
        if targetchan in self.last_e6_md5_links:
            last_md5_links = self.last_e6_md5_links[targetchan]
        else:
            last_md5_links = collections.deque(maxlen=20)
            self.last_e6_md5_links[targetchan] = last_md5_links

        for match in e6matches:
            try:
                last_md5_links.remove(match[1])
            except ValueError:
                pass

            last_md5_links.appendleft(match[1])  # Bring this link to the front

            if opted_out or match[0] == '':
                return

            # They've sent a sample/preview link D:
            try:
                results = await self.e621_search_md5(match[1], line.sourceraw, target)
            except Exception as ex:
                await self.send_log('E621', f"Search failed for \2{match[1]}\2: Exception raised: {type(ex).__name__}: {str(ex)}")
                return

            if type(results) is dict and 'error' in results:
                await self.send_log('E621', f"Search failed for \2{match[1]}\2: Error: {results['error']}")
                return
            await self.send_log('E621', f"Search succeeded for \2{match[1]}\2: {len(results)} post(s) found.")

            for post in results:
                file_url = post['file']['url']
                if file_url is None: continue
                await self.send_message(target, f"[E621/{post['id']}] {line.source['nick']}: Full-resolution image ({post['file']['width']}x{post['file']['height']}): {post['file']['url']}")

    async def handle_pm_command(self, line, command, params):
        source = line.source['nick']
        is_admin = 'account' in line.tags and line.tags['account']['value'].lower() in self.data['admins']

        if command == 'admin' and is_admin:
            await self.handle_admin_command(line, params)
        elif command == 'die' and is_admin:
            msg = "Bot shutting down"
            if len(params) > 0:
                msg = ' '.join(params)
            await self.send_log('bot', f'Received die command from {line.sourceraw} - "{msg}"')
            await self.quit(msg)
        elif command == 'addchan' and is_admin:
            if len(params) < 1 or len(params) > 2:
                await self.send_notice(source, "Usage: addchan <channel> [key]")
                return
            channame = params[0].lower()
            if channame in self.data['channels']:
                await self.send_notice(source, "I'm already in that channel.")
                return
            chan = {}
            if len(params) > 1:
                chan['key'] = params[1]
            self.data['channels'][channame] = chan
            self._save_bot_data()

            if 'key' in chan:
                await self.join(channame, chan['key'])
            else:
                await self.join(channame)
            await self.send_notice(source, f"Successfully added channel \2{params[0]}\2")
            await self.send_log('channel', f"Channel added by {source}: {params[0]}")
        elif command == 'delchan' and is_admin:
            if len(params) != 1:
                await self.send_notice(source, "Usage: delchan <channel>")
                return
            channame = params[0].lower()
            if channame not in self.data['channels']:
                await self.send_notice(source, "I'm not in that channel.")
                return
            self.data['channels'].pop(channame)
            self._save_bot_data()
            await self.write_line(irc.IRCLine(verb='PART', params=[channame]))
            await self.send_notice(source, f"Successfully removed channel \2{params[0]}\2")
            await self.send_log('channel', f"Channel removed by {source}: {params[0]}")
        elif command == 'listchans' and is_admin:
            if len(params) != 0:
                await self.send_notice(source, "Usage: listchans")
                return
            if len(self.data['channels']) == 1:
                await self.send_notice(source, "There is 1 channel.")
            else:
                await self.send_notice(source, f"There are {len(self.data['channels'])} channels.")
            msg = ''
            for channel in self.data['channels']:
                if msg != '':
                    msg += ', '
                msg += channel
                if len(msg) > 300:
                    await self.send_notice(source, "Channel list: " + msg)
                    msg = ''
            if msg != '':
                await self.send_notice(source, "Channel list: " + msg)
        elif command == 'reloadsec' and is_admin:
            if len(params) != 0:
                await self.send_notice(source, "Usage: reloadsec")
                return
            await self.send_notice(source, "Reloading the bot secrets...")
            await self.send_log('optout', f'\2{line.sourceraw}\2 is reloading the bot secrets...')
            with open('secrets.json', 'r') as fp:
                self.__secrets = json.load(fp)
            await self.send_log('optout', f'\2{line.sourceraw}\2 has reloaded the bot secrets.')
            await self.send_notice(source, "Reloaded the bot secrets.")
        elif command == 'config' and is_admin:
            if len(params) < 2:
                await self.send_notice(source, "Usage: config <get|set> <channel> [key] [value...]")
                return
            op = params[0].lower()
            channel = params[1].lower()
            key = params[2].lower()

            if channel not in self.data['channels']:
                await self.send_notice(source, "I'm not on that channel.")
                return

            if op == 'get':
                if len(params) != 3:
                    await self.send_notice(source, "Usage: config get <channel> <key>")
                    return
                if key in self.data['channels'][channel]:
                    await self.send_notice(source, f"On {channel}: {key} = {self.data['channels'][channel][key]}")
                else:
                    await self.send_notice(source, f"{key} is not set on {channel}.")
            elif op == 'set':
                if len(params) == 3:
                    value = None
                elif len(params) > 3:
                    value = ' '.join(params[3:])
                else:
                    await self.send_notice(source, "Usage: config get <channel> <key> [value]")
                    return

                if value is None:
                    if key in self.data['channels'][channel]:
                        self.data['channels'][channel].pop(key)
                    self._save_bot_data()
                    await self.send_notice(source, f"{key} has been unset on {channel}.")
                    await self.send_log('config', f"{line.sourceraw} has unset {key} on {channel}")
                else:
                    self.data['channels'][channel][key] = value
                    self._save_bot_data()
                    await self.send_notice(source, f"{key} has been set to '{value}' on {channel}")
                    await self.send_log('config', f"{line.sourceraw} has set {key} to '{value}' on {channel}")
            else:
                await self.send_notice(source, "Usage: config <get|set> <channel> [key] [value...]")
        elif command == 'clearrecent' and is_admin:
            if len(params) != 0:
                await self.send_notice(source, "Usage: clearrecent")
                return
            self.fa_recent_lookups.clear()
            await self.send_log('FA', f"Recent lookups cleared (requested by {line.sourceraw})")

            self.e6_recent_post_lookups.clear()
            self.e6_recent_md5_lookups.clear()
            await self.send_log('E621', f"Recent post lookups and searches cleared (requested by {line.sourceraw})")
        elif command == 'listoptout' and is_admin:
            if len(params) != 0:
                await self.send_notice(source, "Usage: listoptout")
                return
            if len(self.data['optout']) == 1:
                await self.send_notice(source, "1 user has opted out.")
            else:
                await self.send_notice(source, f"{len(self.data['optout'])} users have opted out.")
            msg = ''
            for optout in self.data['optout']:
                if msg != '':
                    msg += ', '
                msg += optout
                if len(msg) > 300:
                    await self.send_notice(source, "Opt-out list: " + msg)
                    msg = ''
            if msg != '':
                await self.send_notice(source, "Opt-out list: " + msg)
        elif command == 'optout':
            if len(params) != 0:
                await self.send_notice(source, "Usage: optout")
                return
            if 'account' not in line.tags:
                await self.send_notice(source, "Please log in to NickServ so I know who you are.")
                return
            accountname = line.tags['account']['value'].lower()
            if accountname in self.data['optout']:
                await self.send_notice(source, "You have already opted out of the service.")
                return
            self.data['optout'].append(accountname)
            self._save_bot_data()
            await self.send_log('optout', f'\2{accountname}\2 has opted out of the service.')
            await self.send_notice(source, "You have opted out of the service.")
        elif command == 'optin':
            if len(params) != 0:
                await self.send_notice(source, "Usage: optin")
                return
            if 'account' not in line.tags:
                await self.send_notice(source, "Please log in to NickServ so I know who you are.")
                return
            accountname = line.tags['account']['value'].lower()
            if accountname not in self.data['optout']:
                await self.send_notice(source, "You have not opted out of the service.")
                return
            self.data['optout'].remove(accountname)
            self._save_bot_data()
            await self.send_log('optout', f'\2{accountname}\2 has opted back into the service.')
            await self.send_notice(source, "You have opted back into the service.")
        elif command == 'help':
            if len(params) != 0:
                await self.send_notice(source, "Usage: help")
                return
            await self.send_notice(source, "There are 2 commands:")
            await self.send_notice(source, "optout - Opt out of the service. It will no longer automatically respond to messages from users of your NickServ account.")
            await self.send_notice(source, "optin - If you have previously opted out, opt back in to the service.")
        else:
            await self.send_notice(source, f"Invalid command. Try \2/msg {self.nick} help\2 for a list.")

    async def handle_channel_command(self, line, command, params):
        source = line.source['nick']
        target = line.params[0]
        targetchan = target.lower()
        # TODO: add command to grab static1.e621 links and search for their posts

        if command == 'e6md5':
            postsearch = None
            if len(params) == 0:
                postsearch = 1
            elif len(params) == 1:
                try:
                    postsearch = int(params[0])
                except ValueError:
                    pass

                if postsearch is None and re.fullmatch(VALID_MD5, params[0]):
                    postsearch = params[0]

            if postsearch is None:
                await self.send_message(target, f"{source}: Invalid search string. Must be a valid md5 digest or number.")
                return

            if type(postsearch) is int:
                if postsearch < 1:
                    await self.send_message(target, f"{source}: The history index must be 1 or greater.")
                    return
                if targetchan not in self.last_e6_md5_links:
                    await self.send_message(target, f"{source}: I don't remember that many messages in the past.")
                    return

                try:
                    postsearch = self.last_e6_md5_links[targetchan][postsearch-1]
                except IndexError:
                    await self.send_message(target, f"{source}: I don't remember that many messages in the past.")
                    return

            try:
                posts = await self.e621_search_md5(postsearch, source, target)
            except Exception as ex:
                await self.send_log('E621', f"Search failed for \2{postsearch}\2: Exception raised: {type(ex).__name__}: {str(ex)}")
                await self.send_message(target, f"{source}: Error: An exception was raised while searching for the post.")
                return

            if type(posts) is dict and 'error' in posts:
                await self.send_log('E621', f"Search failed for \2{postsearch}\2: Error: {posts['error']}")
                await self.send_message(target, f"{source}: Error: {posts['error']}")
                return
            await self.send_log('E621', f"Search succeeded for \2{postsearch}\2: {len(posts)} post(s) found.")

            if len(posts) == 0:
                await self.send_message(target, f"{source}: No posts were found by that md5 digest.")
                return
            for post in posts:
                poststr = self.e621_create_poststr(post, include_post=True)
                await self.send_message(target, f"{source}: [E621/{post['id']}] {poststr}")

    async def handle_admin_command(self, line, params):
        source = line.source['nick']
        if len(params) == 0:
            await self.send_notice(source, "Usage: admin <add|remove|list> ...")
            return

        subcmd = params[0].lower()
        if subcmd == 'add':
            if len(params) != 2:
                await self.send_notice(source, "Usage: admin add <accountname>")
                return
            adminname = params[1].lower()
            if adminname in self.data['admins']:
                await self.send_notice(source, "That account is already an administrator.")
                return
            self.data['admins'].append(adminname)
            self._save_bot_data()
            await self.send_notice(source, f"\2{params[1]}\2 is now an administrator.")
            await self.send_log('admin', f"Administrator added by {source}: {params[1]}")
        elif subcmd == 'remove':
            if len(params) != 2:
                await self.send_notice(source, "Usage: admin remove <accountname>")
                return
            adminname = params[1].lower()
            if adminname not in self.data['admins']:
                await self.send_notice(source, "That account is not an administrator.")
                return
            self.data['admins'].remove(adminname)
            self._save_bot_data()
            await self.send_notice(source, f"\2{params[1]}\2 is now no longer an administrator.")
            await self.send_log('admin', f"Administrator removed by {source}: {params[1]}")
        elif subcmd == 'list':
            if len(params) != 1:
                await self.send_notice(source, "Usage: admin list")
                return
            if len(self.data['admins']) == 1:
                await self.send_notice(source, "There is 1 administrator.")
            else:
                await self.send_notice(source, f"There are {len(self.data['admins'])} administrators.")
            msg = ''
            for admin in self.data['admins']:
                if msg != '':
                    msg += ', '
                msg += admin

                if len(msg) > 300:
                    await self.send_notice(source, f"Admin list: {msg}")
                    msg = ''
            if msg != '':
                await self.send_notice(source, f"Admin list: {msg}")
        else:
            await self.send_notice(source, "Usage: admin <add|remove|list> ...")
            return
