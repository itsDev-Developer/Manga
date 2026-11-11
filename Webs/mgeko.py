
from .scraper import Scraper
import json

from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, quote, quote_plus

import re
from loguru import logger
from .utitls import DEAULT_MSG_FORMAT, extract_og_image


class MgekoWebs(Scraper):
  def __init__(self):
    super().__init__()
    self.url = "https://www.mgeko.cc/"
    self.bg = None
    self.sf = "mgeko"
    self.headers = {
      "accept": "*/*",
      #"accept-encoding": "gzip, deflate, br, zstd",
      "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
      "connection": "keep-alive",
      "host": "www.mgeko.cc",
      "referer": "https://www.mgeko.cc/",
      "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",

    }

  async def search(self, query: str = ""):
    url = f"https://www.mgeko.cc/autocomplete?term={quote_plus(query)}"
    mangas = await self.get(url, headers=self.headers)
    bs = BeautifulSoup(mangas, "html.parser") if mangas else None
    cards = bs.find_all("li") if bs else None
    results = []
    if cards:
      for card in cards:
        try:
          data = {}
          data['title'] = card.find_next("a")['title']
          
          data['poster'] = card.find_next("img")['src']
          data['url'] = urljoin(self.url, card.find_next("a")['href'])
          
          results.append(data)
        except:
          continue

    return results

  async def get_chapters(self, data, page: int=1):
    results = data

    content = await self.get(results['url'], cs=True)
    bs = BeautifulSoup(content, "html.parser") if content else None
    if bs:
      hi_res_poster = extract_og_image(bs)
      if hi_res_poster:
        results['poster'] = hi_res_poster

      con = bs.find(class_="categories")
      if con:
        gen = ' '.join([con.text.strip() for con in con.find_all("a")])
        
        des = bs.find("p", class_="description").text.strip() if bs.find("p", class_="description") else "N/A"
        
        results['msg'] = DEAULT_MSG_FORMAT.format(
          title=results['title'],
          status="N/A",
          genres=gen,
          summary=des[:200],
          url=results['url']
        )
    
    chapters_url = f"{results['url']}all-chapters/"
    chapters = await self.get(chapters_url, headers=self.headers)
    
    results['chapters'] = chapters if chapters else None
    
    return results

  def iter_chapters(self, data, page: int=1):
    chapters_list = []

    if 'chapters' in data:
      bs = BeautifulSoup(data['chapters'], "html.parser")
      
      ul = bs.find('div', {'id': 'chpagedlist'}) if bs else None

      lis = ul.find_all('li') if ul else None
      if lis:
        for card in lis:
          chapter_slug = card.find_next("a")['title']
          chapter_search = re.search(r"chapter-([\d]+(?:\.[\d]+)?)\-([\w-]+)", chapter_slug)
          chapter_text = f"{chapter_search.group(1)}-{chapter_search.group(2)}" if chapter_search else chapter_slug
          
          chapters_list.append({
            "title": chapter_text,
            "url": urljoin(self.url, card.find_next("a")['href']),
            "manga_title": data['title'],
            "poster": data['poster'] if 'poster' in data else None,
          })
    
    return chapters_list[(page - 1) * 60:page * 60] if page != 1 else chapters_list

  async def get_pictures(self, url, data=None):
    content = await self.get(url, headers=self.headers)
    
    bs = BeautifulSoup(content, "html.parser") if content else None

    ul = bs.find("div", {"id": "chapter-reader"}) if bs else None

    images = ul.find_all('img') if ul else None

    images_url = [quote(img.get('src'), safe=':/%') for img in images] if images else []
    
    return images_url
