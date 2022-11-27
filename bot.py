import asyncio
import urllib.parse

import e6handler
import irc
import json
import time
import re
import collections

import fahandler

VALID_MD5 = re.compile('[\\da-f]{32}', re.IGNORECASE)


def get_tagstr(tag_list):
    tagstr = ''
    added_tags = 0
    for tag in tag_list:
        if tagstr != '':
            tagstr += ', '
        tagstr += tag
        added_tags += 1
        if len(tagstr) > 350:
            break
    return tagstr, f" (and {len(tag_list) - added_tags} more)" if len(tag_list) > added_tags else '', tag_list[added_tags:]


class FABot(irc.SASLIRCBot):
    def __init__(self, config, secrets):
        super().__init__(secrets['sasluser'], secrets['saslpass'], require_auth=config['require_auth'], nick=config['nick'], ident=config['ident'], realname=config['realname'])
        self.__secrets = secrets
        self.logchan = config['logchan']
        self._load_bot_data('bot.json')
        self.onchans = []
        self._last_try_join_time = time.time()
        self._last_e621_api_call = 0

        self.fa_recent_lookups = collections.deque(maxlen=20)
        self.e6_recent_post_lookups = collections.deque(maxlen=20)
        self.e6_recent_md5_lookups = collections.deque(maxlen=20)
        self.e6_recent_search_pages = collections.deque(maxlen=20)

        self.e6_recent_post_replies = {}
        self.e6_tag_more = {}

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

    def add_e621_post_reply(self, chan, post):
        targetchan = chan.lower()
        if targetchan in self.e6_recent_post_replies:
            recents = self.e6_recent_post_replies[targetchan]
        else:
            recents = collections.deque(maxlen=20)
            self.e6_recent_post_replies[targetchan] = recents

        recents.appendleft(post)

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
        is_ctcp = message.startswith('\1') and message.endswith('\1')

        if is_ctcp:
            ctcpmsg = message[1:-1]
            cmd = ctcpmsg.split(' ')[0].upper()
            if cmd == 'VERSION':
                await self.send_notice(line.source['nick'], '\1VERSION FAbot v1 by \\\1')

        if target == self.nick and not is_ctcp:  # PM command
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
                await self.handle_channel_command(line, splt[1].lower(), splt[2:])
            else:
                opted_out = 'account' in line.tags and line.tags['account']['value'].lower() in self.data['optout']

                if not opted_out:
                    await self.handle_furaffinity(message, line, target)
                    await self.handle_e621_posts(message, line, target)
                    await self.handle_e621_static1(message, line, target)

    async def handle_furaffinity(self, message, line, target):
        famatches = list(dict.fromkeys(re.findall(fahandler.FURAFFINITY_POST_PATTERN, message)))
        now = time.time()

        targetchan = target.lower()
        chandata = self.data['channels'][targetchan]
        allow_nsfw = 'nsfw' in chandata and chandata['nsfw'] == 'true'

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
                    #await self.send_message(target, f"[FA/{match}] Error: {info['error']}")
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

                await self.send_log('FA', f"Lookup succeeded for \2{match}\2: {infostr}")

                if info['rating'] == 'General' or allow_nsfw:
                    await self.send_message(target, f"[FA/{match}] {infostr}")
            except Exception as ex:
                await self.send_log('FA', f"Lookup failed for \2{match}\2: Exception raised: {type(ex).__name__}: {str(ex)}")
                #await self.send_message(target, f"[FA/{match}] Error: An exception occurred while parsing the webpage.")

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

        post_blacklisted = False
        if post['rating'] != 's':
            for tag in e6handler.BLACKLIST_SAFE:
                if tag in post['tags']['general']:
                    post_blacklisted = True
                    break

        if not post_blacklisted:
            for tag in e6handler.BLACKLIST_GENERAL:
                if tag in post['tags']['general']:
                    post_blacklisted = True
                    break

        if not post_blacklisted:
            for tag in e6handler.BLACKLIST_GENERAL_POST:
                if tag in post['tags']['general']:
                    post_blacklisted = True
                    break

        poststr = f"[E621/{'(blacklisted)' if post_blacklisted else post['id']}] "

        total_votes = post['score']['up'] + abs(post['score']['down'])
        if total_votes > 0:
            upvote_percent = (post['score']['up'] / total_votes) * 100
        else:
            upvote_percent = 0

        poststr += f"Art by {artists} | Rating: {e6handler.get_rating(post['rating'])} | Score: {post['score']['total']:+} ({upvote_percent:.0f}%) | "
        if post['flags']['deleted']:
            poststr += "Post is deleted | "
        elif post['flags']['flagged']:
            poststr += "Post is flagged for deletion | "

        if include_post:
            if post_blacklisted:
                poststr += f"Post: (blacklisted) | "
            else:
                poststr += f"Post: https://{'e926' if post['rating'] == 's' else 'e621'}.net/posts/{post['id']} | "

        content_warning = set()
        if post['rating'] != 's':
            for bls in e6handler.BLACKLIST_SAFE:
                if bls in post['tags']['general']:
                    content_warning.add(bls)
        for bl in e6handler.BLACKLIST_GENERAL:
            if bl in post['tags']['general']:
                content_warning.add(bl)
        for blp in e6handler.BLACKLIST_GENERAL_POST:
            if blp in post['tags']['general']:
                content_warning.add(blp)
        for cw in e6handler.CONTENT_WARNING_GENERAL:
            if cw in post['tags']['general']:
                content_warning.add(cw)

        if 'file' in post:
            file_obj = post['file'] or {'width': None, 'height': None, 'url': None}
            file_url = file_obj['url']
            if file_url is not None and post_blacklisted:
                file_url = "(blacklisted)"
            if file_url is not None and post['rating'] == 's':
                file_url = file_url.replace("static1.e621.net", "static1.e926.net", 1)
            poststr += f"Image ({file_obj['width'] or '?'}x{file_obj['height'] or '?'}): {file_url or '(unknown)'}"
        else:
            poststr += f"Image (?x?): (unknown)"

        if len(content_warning) > 0:
            poststr += f" | CW: {', '.join(content_warning)}"

        return poststr

    async def e621_ratelimit_wait(self):
        now = time.time()
        since_last = now - self._last_e621_api_call
        if since_last < 0.6:
            await asyncio.sleep(0.6 - since_last)

    async def handle_e621_posts(self, message, line, target):
        e6matches = list(dict.fromkeys(re.findall(e6handler.E621_POST_PATTERN, message)))
        now = time.time()

        targetchan = target.lower()
        chandata = self.data['channels'][targetchan]
        allow_nsfw = 'nsfw' in chandata and chandata['nsfw'] == 'true'

        for match in e6matches:
            try:
                post = None
                for lookup in self.e6_recent_post_lookups:
                    if lookup[0] == match and now - lookup[1] < 300:
                        await self.send_log('E621', f"Found cached post \2{match}\2 (requested by {line.sourceraw} in {target})")
                        post = lookup[2]

                if post is None:
                    await self.send_log('E621', f"Looking up post \2{match}\2 (requested by {line.sourceraw} in {target})")
                    await self.e621_ratelimit_wait()
                    now = time.time()
                    post = e6handler.get_post_info(self.__secrets['auth']['e621'], match)
                    self._last_e621_api_call = now
                    self.e6_recent_post_lookups.append((match, now, post))

                if 'error' in post:
                    await self.send_log('E621', f"Lookup failed for \2{match}\2: Error: {post['error']}")
                    await self.send_message(target, f"[E621/{match}] Error: {post['error']}")
                    continue

                poststr = self.e621_create_poststr(post)
                await self.send_log('E621', f"Lookup succeeded for \2{match}\2: {poststr}")

                if allow_nsfw or post['rating'] == 's':
                    self.add_e621_post_reply(targetchan, post)
                    await self.send_message(target, poststr)
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
            await self.e621_ratelimit_wait()
            now = time.time()
            results = e6handler.search_post_hash(self.__secrets['auth']['e621'], md5_hash)
            self.e6_recent_md5_lookups.append((md5_hash, now, results))
        return results

    async def handle_e621_static1(self, message, line, target):
        e6matches = list(dict.fromkeys(re.findall(e6handler.E621_IMAGE_PATTERN, message)))
        targetchan = target.lower()
        chandata = self.data['channels'][targetchan]
        allow_nsfw = 'nsfw' in chandata and chandata['nsfw'] == 'true'

        for match in e6matches:
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
                if post['rating'] != 's' and not allow_nsfw:
                    continue

                poststr = self.e621_create_poststr(post, include_post=True)
                self.add_e621_post_reply(targetchan, post)
                await self.send_message(target, poststr)

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
            self.e6_recent_search_pages.clear()
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
            await self.send_notice(source, "See https://github.com/bigfoot547/FAbot/blob/master/README.md")
        else:
            await self.send_notice(source, f"Invalid command. Try \2/msg {self.nick} help\2 for a list.")

    async def handle_channel_command(self, line, command, params):
        source = line.source['nick']
        target = line.params[0]
        targetchan = target.lower()
        chandata = self.data['channels'][targetchan]
        allow_nsfw = 'nsfw' in chandata and chandata['nsfw'] == 'true'

        if command == 'e6md5':
            postsearch = None
            if len(params) != 1:
                await self.send_message(target, f"{source}: Invalid search string. Must be a valid md5 digest.")
                return
            elif len(params) == 1:
                postsearch = params[0]

            if not re.fullmatch(VALID_MD5, postsearch):
                await self.send_message(target, f"{source}: Invalid search string. Must be a valid md5 digest.")
                return

            try:
                posts = await self.e621_search_md5(postsearch, line.sourceraw, target)
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

            suppressed_results = 0
            for post in posts:
                if post['rating'] != 's' and not allow_nsfw:
                    suppressed_results += 1
                    continue

                poststr = self.e621_create_poststr(post, include_post=True)
                self.add_e621_post_reply(targetchan, post)
                await self.send_message(target, f"{source}: {poststr}")

            if suppressed_results > 0:
                await self.send_message(target, f"{source}: {suppressed_results} NSFW result{'' if suppressed_results == 1 else 's'} suppressed.")
        elif command == "search":
            if len(params) == 0:
                await self.send_message(target, f"{source}: Please supply a search query.")
            query = ' '.join(params)
            qquery = urllib.parse.quote_plus(query, safe='', encoding='utf-8', errors='replace')
            if allow_nsfw:
                await self.send_message(target, f"{source}: Search for '{query}': https://www.furaffinity.net/search/?q={qquery} | https://e621.net/posts?tags={qquery}")
            else:
                await self.send_message(target, f"{source}: Search for '{query}': https://e926.net/posts?tags={qquery}")
        elif command == 'random' or command == 'e6random' or command == 'rnd' or command == 'e6rnd':
            tags = ' '.join(params)
            await self.send_log('E621', f"Searching for random post with tags \2{tags}\2 (requested by {line.sourceraw} in {target})")
            await self.e621_ratelimit_wait()

            try:
                random_post = e6handler.search_post_random(self.__secrets['auth']['e621'], tags, not allow_nsfw)
            except Exception as ex:
                await self.send_log('E621', f"Random search failed for \2{tags}\2: Exception raised: {type(ex).__name__}: {str(ex)}")
                await self.send_message(target, f"{source}: Error: An exception was raised while querying a random post.")
                return

            if 'error' in random_post:
                await self.send_log('E621', f"Random search failed for \2{tags}\2: {random_post['error']}")
                await self.send_message(target, f"{source}: Error: {random_post['error']}")
                return

            poststr = self.e621_create_poststr(random_post, include_post=True)
            await self.send_log('E621', f"Random search succeeded for \2{tags}\2: {poststr}")
            self.add_e621_post_reply(targetchan, random_post)
            await self.send_message(target, f"{source}: {poststr}")
        elif command == 'e6search':
            resnum = 1
            tags_arr = None
            search_forcesafe = False
            if len(params) > 0 and params[0].isnumeric():
                resnum = int(params[0])
                if resnum <= 0:
                    await self.send_message(target, f"{source}: Invalid result number. Must be greater than 0.")
                    return
                tags_arr = params[1:]
            if tags_arr is None:
                tags_arr = params

            tags = ' '.join(tags_arr)

            for tag in tags_arr:
                if tag.lower() in e6handler.BLACKLIST_SAFE:
                    search_forcesafe = True

            resnum -= 1  # Turn it into a 0-based index
            pageidx = int(resnum / 100)
            residx = resnum % 100

            if pageidx < 0 or pageidx >= 750:
                await self.send_message(target, f"{source}: Invalid page number. The result must fall before page 751.")
                return

            await self.send_log('E621', f"Searching for result \2{resnum}\2 of search \2{tags}\2 (requested by {line.sourceraw} in {target})")

            now = time.time()
            page_results = None
            for lookup in self.e6_recent_search_pages:
                if lookup[0] == tags and lookup[1] == pageidx and now - lookup[2] < 300:
                    page_results = lookup[3]
                    await self.send_log('E621', f"Found cached page: {len(page_results)} result(s) (requested by {line.sourceraw} in {target})")
                    break

            if page_results is None:
                await self.e621_ratelimit_wait()
                now = time.time()
                try:
                    page_results = e6handler.search_post_tags(self.__secrets['auth']['e621'], tags, search_forcesafe or not allow_nsfw, pageidx=pageidx)
                except Exception as ex:
                    await self.send_log('E621', f"Search failed: Exception raised: {type(ex).__name__}: {str(ex)}")
                    await self.send_message(target, f"Error: An exception was raised while searching for the post.")
                    return
                self.e6_recent_search_pages.append((tags, pageidx, now, page_results))

            if 'error' in page_results:
                await self.send_log('E621', f"Search failed: {page_results['error']}")
                await self.send_message(target, f"Error: {page_results['error']}")
                return

            await self.send_log('E621', f"Search succeeded: {len(page_results)} result(s) found.")
            if residx >= len(page_results):
                await self.send_message(target, "There are not that many results for that search.")
                return

            post = page_results[residx]
            poststr = self.e621_create_poststr(post, include_post=True)
            self.add_e621_post_reply(targetchan, post)
            await self.send_message(target, f"{source}: {poststr}")
        elif command == 'e6tags':
            if len(params) == 1 and params[0] == '+':
                if targetchan not in self.e6_tag_more:
                    await self.send_message(target, f"{source}: There are no more tags to display.")
                    return

                more = self.e6_tag_more[targetchan]
                if len(more[0]) == 0:
                    await self.send_message(target, f"{source}: There are no more tags to display.")
                    return

                tag_list = more[0]
                post_id = more[1]
                more = True
            else:
                histidx = 1
                if len(params) == 1:
                    try:
                        histidx = int(params[0])
                    except ValueError:
                        await self.send_message(target, f"{source}: The history index must be an integer greater than or equal to 1.")
                        return
                elif len(params) > 1:
                    await self.send_message(target, f"{source}: Usage: e6tags [histidx]|+")

                if histidx < 1:
                    await self.send_message(target, f"{source}: The history index must be an integer greater than or equal to 1.")
                    return

                histidx -= 1  # convert to 0-based index

                post = None
                if targetchan in self.e6_recent_post_replies:
                    recents = self.e6_recent_post_replies[targetchan]
                    if len(recents) > histidx:
                        post = recents[histidx]

                if not post:
                    await self.send_message(target, f"{source}: I don't remember that many recent posts.")
                    return

                tags = set()
                for key in post['tags']:
                    for tag in post['tags'][key]:
                        tags.add(tag)
                tag_list = sorted(tags)
                post_id = post['id']
                more = False

            tagstr, extrastr, leftover = get_tagstr(tag_list)

            await self.send_message(target, f"{source}: [E621/{post_id}{'+' if more else ''}] {tagstr}{extrastr}")
            self.e6_tag_more[targetchan] = (leftover, post_id)
        elif command == 'help':
            if len(params) != 0:
                await self.send_message(target, f"{source}: Usage: help")
                return
            await self.send_message(target, f"{source}: See https://github.com/bigfoot547/FAbot/blob/master/README.md")

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
