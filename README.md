# FAbot
a bot

## Channel Commands
You can query the bot with `FAbot: <command> [arguments...]`, or by using a channel-specific channel prefix, if configured.

- `e6md5 <hash>`
  - Displays the post associated with a certain e621 image md5.
- `search <tag1> [tag2] ...`
  - Shows the e621 and FA search links associated with the specified query. In a SFW channel, only an e926 link is displayed.
- `random|e6random|rnd|e6rnd [tag1] [tag2] ...`
  - Displays a random post from e621 (or e926 if the channel is SFW) matching the specified tags.
- `e6search [resultnum] [tag1] [tag2] ...`
  - Displays the `[resultnum]`th result of the search specified by the tags.
- `e6tags [+]`
  - Displays some tags associated with the previous image reply. Specifying a `+` as the first argument will continue sending the tag list it was displaying before.

## PM Commands
You can send the bot private messages to instruct it as well.

- `optout`
  - The bot will no longer automatically reply to your messages containing fa/e621/static1.e621 links. You must be authenticated to NickServ.
- `optin`
  - Basically undoes an `optout` invocation.
