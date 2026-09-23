#!/usr/bin/env python3
from __future__ import annotations
import hashlib, html, json, logging, os, re, sys, time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT=Path(__file__).resolve().parent; DOCS=ROOT/'docs'; STATE=DOCS/'state.json'; INDEX=DOCS/'index.html'
TOKEN=os.getenv('TELEGRAM_BOT_TOKEN','').strip(); CHAT=os.getenv('TELEGRAM_CHAT_ID','').strip()
MIN_DROP=Decimal(os.getenv('MIN_DROP_AMOUNT','1.00')); UA='DDR5PriceMonitor/1.0 (+personal-use; contact: repository-owner)'

@dataclass
class Result:
    product_id:str; name:str; platform:str; capacity:str; url:str; currency:str
    price:str|None; stock:str; checked_at:str; ok:bool; error:str|None=None

def now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
def esc(v): return html.escape(str(v or ''),quote=True)
def atomic(path:Path,text:str):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+'.tmp'); tmp.write_text(text,encoding='utf-8'); tmp.replace(path)
def load(path:Path,default:Any):
    try: return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default
    except (OSError,json.JSONDecodeError) as e: logging.error('Cannot read %s: %s',path,e); return default

def session():
    r=Retry(total=2,connect=2,read=2,status=2,backoff_factor=1.5,status_forcelist=(429,500,502,503,504),allowed_methods=frozenset({'GET','POST'}),respect_retry_after_header=True,raise_on_status=False)
    s=requests.Session(); s.headers.update({'User-Agent':UA,'Accept':'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8','Accept-Language':'en-IN,en;q=0.8'}); s.mount('https://',HTTPAdapter(max_retries=r)); return s

def host_allowed(host:str,domains:list[str]):
    host=host.lower().rstrip('.'); return any(host==d.lower() or host.endswith('.'+d.lower()) for d in domains)
def validate_url(url:str,domains:list[str]):
    p=urlparse(url)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.port not in (None,443): raise ValueError('Only credential-free HTTPS URLs on port 443 are allowed')
    if not host_allowed(p.hostname,domains): raise ValueError(f'Domain not allowlisted: {p.hostname}')
def robots_ok(s,url):
    p=urlparse(url); ru=f'https://{p.hostname}/robots.txt'
    try:
        r=s.get(ru,timeout=(10,20),allow_redirects=False)
        if r.status_code==404:return True
        r.raise_for_status(); rp=RobotFileParser(); rp.parse(r.text.splitlines()); return rp.can_fetch(UA,url)
    except requests.RequestException as e: logging.warning('robots.txt unavailable: %s',e); return False

def fetch(s,p):
    validate_url(p['url'],p['allowed_domains'])
    if p.get('respect_robots_txt',True) and not robots_ok(s,p['url']): raise RuntimeError('robots.txt disallows or could not verify access')
    url=p['url']
    for _ in range(4):
        r=s.get(url,timeout=(10,25),allow_redirects=False)
        if not r.is_redirect: break
        url=requests.compat.urljoin(r.url,r.headers.get('Location','')); validate_url(url,p['allowed_domains'])
    else: raise RuntimeError('Too many redirects')
    r.raise_for_status(); ctype=r.headers.get('Content-Type','').lower()
    if 'html' not in ctype and 'xhtml' not in ctype: raise RuntimeError(f'Unexpected Content-Type: {ctype or "missing"}')
    if len(r.content)>8_000_000: raise RuntimeError('Response exceeds 8 MB')
    sample=r.text[:200000].lower()
    if any(x in sample for x in ('captcha','verify you are human','robot check','access denied')): raise RuntimeError('Anti-automation page returned')
    return r

def first(soup,selectors):
    for q in selectors:
        n=soup.select_one(q)
        if n:
            v=' '.join(str(n.get('content') or n.get_text(' ',strip=True)).split())
            if v:return v[:500]
    return None
def price(v):
    for m in re.findall(r'\d[\d,]*(?:\.\d{1,2})?',v or ''):
        try:
            d=Decimal(m.replace(',','')).quantize(Decimal('.01'))
            if 0<d<10_000_000:return d
        except InvalidOperation:pass
    return None
def ld(soup):
    for t in soup.select('script[type="application/ld+json"]'):
        try:d=json.loads(t.string or t.get_text())
        except (json.JSONDecodeError,TypeError):continue
        for root in d if isinstance(d,list) else [d]:
            if not isinstance(root,dict):continue
            for x in [root]+(root.get('@graph',[]) if isinstance(root.get('@graph'),list) else []):
                if isinstance(x,dict) and x.get('@type')=='Product':
                    o=x.get('offers',{}); o=(next((z for z in o if isinstance(z,dict)),{}) if isinstance(o,list) else o); o=o if isinstance(o,dict) else {}
                    return str(x.get('name','')).strip()[:500] or None,price(str(o.get('price',''))),str(o.get('availability','')).lower()
    return None,None,None
def stock(soup,p,av):
    page=' '.join(soup.get_text(' ',strip=True).lower().split()); outs=[x.lower() for x in p['out_of_stock_phrases']]; ins=[x.lower() for x in p['in_stock_phrases']]
    for q in p.get('out_of_stock_selectors',[]):
        n=soup.select_one(q)
        if n and any(x in n.get_text(' ',strip=True).lower() for x in outs):return 'out_of_stock'
    for q in p.get('in_stock_selectors',[]):
        n=soup.select_one(q)
        if n and any(x in n.get_text(' ',strip=True).lower() for x in ins):return 'in_stock'
    if av and ('outofstock' in av or 'soldout' in av):return 'out_of_stock'
    if av and ('instock' in av or 'limitedavailability' in av):return 'in_stock'
    if any(x in page for x in outs):return 'out_of_stock'
    if any(x in page for x in ins):return 'in_stock'
    return 'unknown'
def scrape(s,p):
    try:
        soup=BeautifulSoup(fetch(s,p).text,'html.parser'); ln,lp,av=ld(soup); pr=price(first(soup,p['price_selectors'])) or lp
        return Result(p['id'],ln or first(soup,p['name_selectors']) or p['name'],p['platform'],p['capacity'],p['url'],p['currency'],str(pr) if pr else None,stock(soup,p,av),now(),True)
    except Exception as e:return Result(p['id'],p['name'],p['platform'],p['capacity'],p['url'],p['currency'],None,'unknown',now(),False,f'{type(e).__name__}: {e}'[:500])
def evaluate(r,old):
    if not old or not r.ok or r.stock!='in_stock':return None,None
    if old.get('stock')=='out_of_stock':return 'restock',None
    try:
        op=Decimal(str(old['price'])); np=Decimal(str(r.price))
        if old.get('stock')=='in_stock' and op-np>=MIN_DROP:return 'price_drop',op
    except (KeyError,TypeError,InvalidOperation):pass
    return None,None
def money(v,c):
    if not v:return 'Unavailable'
    symbol={'INR':'₹','USD':'$','EUR':'€','GBP':'£'}.get(c,c+' ')
    return f'{symbol}{Decimal(v):,.2f}'
def alert(s,r,why,old):
    if not TOKEN or not CHAT:return False
    title='Back in stock' if why=='restock' else 'Price drop while in stock'; lines=[f'<b>{title}</b>','',f'<b>Product:</b> {html.escape(r.name)}',f'<b>Platform:</b> {html.escape(r.platform)}',f'<b>Price:</b> {money(r.price,r.currency)}','<b>Status:</b> In Stock']
    if old is not None:lines.append(f'<b>Previous:</b> {money(str(old),r.currency)}')
    payload={'chat_id':CHAT,'text':'\n'.join(lines),'parse_mode':'HTML','disable_web_page_preview':True,'reply_markup':{'inline_keyboard':[[{'text':'View product','url':r.url}]]}}
    try:
        x=s.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',json=payload,timeout=(10,25)); x.raise_for_status(); return bool(x.json().get('ok'))
    except Exception as e:logging.error('Telegram failed: %s',e); return False
def validate(ps):
    req={'id','name','platform','capacity','url','currency','allowed_domains','name_selectors','price_selectors','in_stock_phrases','out_of_stock_phrases'}; seen=set()
    if not isinstance(ps,list) or not ps:raise ValueError('products.json must be a non-empty array')
    for p in ps:
        if not isinstance(p,dict) or req-set(p):raise ValueError(f'Invalid product entry: missing {sorted(req-set(p) if isinstance(p,dict) else req)}')
        if p['id'] in seen or not re.fullmatch(r'[a-z0-9][a-z0-9-]{2,63}',p['id']):raise ValueError(f'Invalid or duplicate id: {p["id"]}')
        if p['capacity'] not in {'8GB','16GB'}:raise ValueError('Only 8GB or 16GB is allowed')
        validate_url(p['url'],p['allowed_domains']); seen.add(p['id'])
    return ps
def merge(r,old):
    d=asdict(r)
    if not r.ok and old:d['price']=old.get('price');d['stock']=old.get('stock','unknown');d['last_successful_check']=old.get('last_successful_check')
    else:d['last_successful_check']=r.checked_at
    return d
def render(st):
    cards=[]
    for i in sorted(st['products'].values(),key=lambda x:(x['capacity'],x['platform'],x['name'])):
        status=i.get('stock','unknown'); label={'in_stock':'In Stock','out_of_stock':'Out of Stock'}.get(status,'Unknown'); cls={'in_stock':'good','out_of_stock':'bad'}.get(status,'warn'); err=f'<p class="error">Latest check: {esc(i.get("error"))}</p>' if i.get('error') else ''
        cards.append(f'<article class="card"><div class="meta"><span>{esc(i["platform"])}</span><span>DDR5 {esc(i["capacity"])}</span></div><h2>{esc(i["name"])}</h2><p class="price">{esc(money(i.get("price"),i["currency"]))}</p><span class="badge {cls}">{label}</span>{err}<a class="button" href="{esc(i["url"])}" target="_blank" rel="noopener noreferrer nofollow">View product</a></article>')
    u=esc(st['updated_at']); css='*{box-sizing:border-box}body{margin:0;background:#f8fafc;color:#0f172a;font:16px system-ui}main{max-width:1200px;margin:auto;padding:32px 18px}header{background:#0f172a;color:white;padding:28px;border-radius:24px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:18px;margin-top:22px}.card{background:white;border:1px solid #e2e8f0;border-radius:20px;padding:20px}.meta{display:flex;gap:8px}.meta span{background:#eef2ff;color:#3730a3;border-radius:99px;padding:5px 9px;font-size:12px}.price{font-size:26px;font-weight:800}.badge{display:inline-block;border-radius:99px;padding:6px 10px;font-weight:700}.good{background:#d1fae5;color:#065f46}.bad{background:#fee2e2;color:#991b1b}.warn{background:#fef3c7;color:#92400e}.button{display:block;text-align:center;margin-top:18px;padding:11px;background:#4f46e5;color:white;text-decoration:none;border-radius:11px}.error{background:#fffbeb;color:#92400e;padding:9px;border-radius:9px;font-size:12px;overflow-wrap:anywhere}'
    return f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'; frame-ancestors \'none\'"><meta name="referrer" content="no-referrer"><title>DDR5 RAM Monitor</title><style>{css}</style></head><body><main><header><h1>DDR5 RAM Price and Stock Monitor</h1><p>Last refreshed: <time datetime="{u}">{u}</time></p></header><section class="grid">{"".join(cards)}</section></main></body></html>'
def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)sZ %(levelname)s %(message)s'); logging.Formatter.converter=time.gmtime
    try:ps=validate(load(ROOT/'products.json',[]))
    except Exception as e:logging.critical('Configuration rejected: %s',e);return 2
    st=load(STATE,{'schema':1,'updated_at':None,'products':{}}); st=st if isinstance(st,dict) and isinstance(st.get('products'),dict) else {'schema':1,'updated_at':None,'products':{}}
    s=session();ok=0
    for n,p in enumerate(ps):
        if n:time.sleep(2)
        r=scrape(s,p); old=st['products'].get(r.product_id); why,op=evaluate(r,old)
        if why:alert(s,r,why,op)
        st['products'][r.product_id]=merge(r,old);ok+=int(r.ok)
    st['updated_at']=now(); page=render(st);st['dashboard_sha256']=hashlib.sha256(page.encode()).hexdigest();atomic(STATE,json.dumps(st,indent=2,sort_keys=True,ensure_ascii=False)+'\n');atomic(INDEX,page)
    return 0 if ok else 1
if __name__=='__main__':raise SystemExit(main())
