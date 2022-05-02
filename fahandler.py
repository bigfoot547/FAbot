import cfscrape
import bs4
import re
import urllib.parse

FURAFFINITY_POST_PATTERN = re.compile("furaffinity\\.net/(?:view|full)/(\\d+)", re.IGNORECASE)

scraper = cfscrape.create_scraper()

# Code adapted from https://github.com/Hidoni/FAToFACDN/blob/master/furaffinityhandler.py


def get_info(secrets, post_id):
    myusername = secrets['username']
    scraper.get("https://www.furaffinity.net/")
    scraper.cookies.update(secrets['cookies'])
    post_url = f'https://www.furaffinity.net/view/{urllib.parse.quote(post_id, safe="", encoding="utf-8", errors="replace")}/'
    response = scraper.get(post_url)
    if response.status_code == 404:
        return {'error': "Post not found"}
    elif response.status_code != 200:
        return {'error': f"Server responded with {response.status_code} {response.reason}"}

    content = response.content
    soup = bs4.BeautifulSoup(content, 'html.parser')

    info = {}

    if soup.title.get_text() == 'System Error':
        return {'error': "Post not found"}
    if soup.find('div', class_="audio-player-container") or soup.find('div', class_="font-size-panel"):
        return {'error': "URL points to an audio or story post"}

    if myusername:
        found = False
        for img in soup.findAll('img', class_='loggedin_user_avatar'):
            if img.has_attr('alt') and img['alt'] == myusername:
                found = True
                break
        if not found:
            return {'error': "Not logged in (invalid cookies?)"}

    submit_img = soup.find('img', id='submissionImg')
    if submit_img is not None and submit_img.has_attr('data-fullview-src'):
        info['cdn-link'] = urllib.parse.urljoin(post_url, submit_img['data-fullview-src'])
        info['title'] = submit_img['alt']
    else:
        for download_div in soup.findAll('div', class_='download'):
            link = download_div.a
            if link is not None and link.has_attr('href'):
                info['download-link'] = urllib.parse.urljoin(post_url, link['href'])

    info['artist'] = '(unknown)'
    for link in soup.findAll('a', href=re.compile('^/user/')):
        key = link.find('strong')
        if key and key.contents:
            info['artist'] = key.contents[0]
            break

    info['rating'] = '(unknown)'
    for div in soup.findAll('div', class_='rating'):
        rating_box = div.find('span', class_='rating-box')
        if rating_box:
            info['rating'] = rating_box.get_text().strip(' ')
    return info
