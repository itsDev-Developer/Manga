from .scraper import Scraper
import json

from bs4 import BeautifulSoup
from urllib.parse import urlparse, urljoin, quote, quote_plus

import re
from loguru import logger
from .utitls import DEAULT_MSG_FORMAT, extract_og_image


class Manhwa18Webs(Scraper):
  def __init__(self):
    super().__init__()
    self.url = "https://manhwa18.cc/"
    self.bg = None
    self.sf = "ma18"
    self.headers = {
      "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    }

  async def search(self, query: str = ""):
    url = f"https://manhwa18.cc/search?q={quote_plus(query)}"
    mangas = await self.get(url)

    bs = BeautifulSoup(mangas, "html.parser") if mangas else None

    container = bs.find('div', {'class': 'manga-lists'}) if bs else None

    cards = container.find_all("div", {"class": "manga-item"}) if container else None

    results = []
    for card in cards:
      data = {}
      data['url'] = urljoin(self.url, card.findNext("a").get("href"))
      data['title'] = card.findNext("a").get("title")

      data['poster'] = card.findNext("img").get("src")
      
      results.append(data)

    return results

  async def get_chapters(self, data, page: int=1):
    results = data

    content = await self.get(results['url'])
    bs = BeautifulSoup(content, "html.parser") if content else None

    container = bs.find(class_="genres-content") if bs else None
    geners = container.text.strip() if container else "N/A"

    container = bs.find(class_="dsct")
    des = container.text.strip() if container else "N/A"

    results['msg'] = DEAULT_MSG_FORMAT.format(
      title=results['title'],
      status="N/A",
      genres=geners if geners else "N/A",
      summary=des[:400] if des else "N/A",
      url=results['url']
    )
    _poster = bs.find(class_="summary_image")
    hi_res_poster = extract_og_image(bs) or (_poster.findNext("img").get("src") if _poster else None)
    if hi_res_poster:
      results['poster'] = hi_res_poster
    
    chapters = bs.find("ul", {"class": "row-content-chapter"})
    chapters = chapters.find_all("li", {"class": "a-h"}) if chapters else []

    results['chapters'] = chapters

    return results


  def iter_chapters(self, data, page: int=1):
    chapters_list = []

    if 'chapters' in data:
     for card in data['chapters']:
       chapters_list.append({
         "title": card.find_next("a").text.strip(),
         "url": urljoin(self.url, card.find_next("a")['href']),
         "manga_title": data['title'],
         "poster": data['poster'] if 'poster' in data else None,
         })

    return chapters_list[(page - 1) * 60:page * 60] if page != 1 else chapters_list

  async def get_pictures(self, url, data=None):
    content = await self.get(url)

    bs = BeautifulSoup(content, "html.parser")

    container = bs.find("div", {"class": "read-content wleft tcenter"}) if bs else None
    cards = container.find_all("img") if container else None
    
    images_url = [quote(containers.get("src"), safe=':/%') for containers in cards] if cards else None
    
    return images_url

  
