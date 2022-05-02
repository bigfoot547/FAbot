import json
import re
import requests
import requests.auth
import urllib.parse
import traceback

# https://e926.net/posts/3309661
# https://e926.net/post/show/3309661
# 2847950
E621_POST_PATTERN = re.compile("e(?:621|926)\\.net/(?:posts|post/show)/(\\d+)", re.IGNORECASE)
E621_IMAGE_PATTERN = re.compile("static1\\.e(?:621|926)\\.net/data/(preview/|sample/)?[\\da-f]{2}/[\\da-f]{2}/([\\da-f]+)\\.[a-z]+", re.IGNORECASE)
USER_AGENT = "FAbot/0.1 (by one_two_oatmeal on e621)"
RATINGS = {'s': "Safe", 'q': "Questionable", 'e': "Explicit"}


def get_rating(key):
    if key in RATINGS:
        return RATINGS[key]
    else:
        return f"Unknown ({key})"


def get_post_info(secrets, post_id):
    post_url = f"https://e621.net/posts/{urllib.parse.quote(post_id, safe='', encoding='utf-8', errors='replace')}.json"
    response = requests.get(post_url,
                            headers={'User-Agent': USER_AGENT},
                            auth=requests.auth.HTTPBasicAuth(secrets['username'], secrets['api_key']))
    if response.status_code == 404:
        return {'error': "Post not found"}
    elif response.status_code != 200:
        return {'error': f"Server responded with {response.status_code} {response.reason}"}

    try:
        return response.json()['post']
    except json.JSONDecodeError as ex:
        traceback.print_exception(type(ex), ex, ex.__traceback__)
        return {'error': "Unable to decode response from server (please contact the bot owner immediately)"}


def search_post_hash(secrets, md5_hash):
    search_url = f"https://e621.net/posts.json?tags={urllib.parse.quote(f'md5:{md5_hash} status:any', safe='', encoding='utf-8', errors='replace')}"
    response = requests.get(search_url,
                            headers={'User-Agent': USER_AGENT},
                            auth=requests.auth.HTTPBasicAuth(secrets['username'], secrets['api_key']))
    if response.status_code != 200:
        return {'error': f"Server responded with {response.status_code} {response.reason}"}

    try:
        return response.json()['posts']
    except json.JSONDecodeError as ex:
        traceback.print_exception(type(ex), ex, ex.__traceback__)
        return {'error': "Unable to decode response from server (please contact the bot owner immediately)"}
