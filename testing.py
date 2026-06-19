# import re
# import requests
# from xml.etree import ElementTree as ET

# base = 'https://www.peoplenewstime.com'
# headers = {'User-Agent': 'Mozilla/5.0'}

# def get(url):
#     try:
#         return requests.get(url, timeout=15, headers=headers)
#     except Exception as e:
#         print(f'  ERR  {url}  ->  {e}')
#         return None

# print('1) Reading robots.txt for declared sitemaps...')
# r = get(base + '/robots.txt')
# sitemap_urls = []
# if r and r.status_code == 200:
#     sitemap_urls = re.findall(r'Sitemap:\s*(\S+)', r.text)
#     for s in sitemap_urls:
#         print(f'  found: {s}')
# else:
#     print(f'  -- HTTP {r.status_code if r else "?"}')
# print()

# print('2) Fetching each declared sitemap...')
# NS = {
#     'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9',
#     'news': 'http://www.google.com/schemas/sitemap-news/0.9',
# }

# all_articles = []
# for sm_url in sitemap_urls:
#     r = get(sm_url)
#     if not r or r.status_code != 200:
#         print(f'  --   {sm_url}  ->  HTTP {r.status_code if r else "?"}')
#         continue
#     try:
#         root = ET.fromstring(r.content)
#     except ET.ParseError as e:
#         print(f'  ERR  {sm_url}  ->  parse failed: {e}')
#         continue

#     urls = root.findall('sm:url', NS)
#     print(f'  HIT  {sm_url}  ->  {len(urls)} <url> entries')

#     for u in urls:
#         loc = u.findtext('sm:loc', namespaces=NS)
#         title = u.findtext('news:news/news:title', namespaces=NS)
#         pub_date = u.findtext('news:news/news:publication_date', namespaces=NS)
#         all_articles.append({'url': loc, 'title': title, 'published': pub_date})

# print()
# print(f'Total articles collected: {len(all_articles)}')
# for a in all_articles[:10]:
#     print(f"  [{a['published']}] {a['title']}")
#     print(f"    {a['url']}")




# diagnose_hf.py
# # testing.py
# from dotenv import load_dotenv
# load_dotenv()
# import sys
# sys.path.insert(0, '.')
# from news_agent.core.config import AppConfig
# from news_agent.services.internalLink.embeddings import create_embeddings_client

# config = AppConfig.from_env()
# client = create_embeddings_client(config)
# if client is None:
#     print("No client — HUGGINGFACE_API_KEY missing")
# else:
#     print(f"Model:    {client.model}")
#     vec = client.embed("Lionel Messi wins another trophy")
#     print(f"Vector length: {len(vec)}")
#     print(f"Last error:    {client.last_error}")



# diagnose_retrieval.py
from dotenv import load_dotenv
load_dotenv()
import sys
sys.path.insert(0, '.')

from news_agent.core.config import AppConfig
from news_agent.services.internalLink import create_vector_store, RetrievalService
from app.cloud_sync import CloudSync

config = AppConfig.from_env()
cloud_sync = CloudSync.instance()
cloud_sync.initialize()

vector_store = create_vector_store(config, cloud_sync=cloud_sync)
tenant_id = config.tenant_id or "_default"
print(f"Store count: {vector_store.count(tenant_id)}")

retrieval = RetrievalService(config, vector_store)
hf = retrieval.hf_client
print(f"hf_client available: {hf is not None}")

query_embedding = hf.embed("Messi") if hf else None
print(f"Query embedding length: {len(query_embedding) if query_embedding else 0}")

if query_embedding:
    candidates = vector_store.search(tenant_id, query_embedding, limit=10)
    print(f"Raw search() candidates: {len(candidates)}")
    for c in candidates[:3]:
        print(f"  {c['score']:.3f}  {c['title']}")

# diagnose_retrieval2.py — append to your existing diagnose_retrieval.py, after the search() block

print()
print("--- Scoring stage ---")
scored = retrieval._score_candidates(
    type("T", (), {"keyword": "Messi"})(),  # quick stand-in if you don't have `topic` in scope
    candidates
)
scored.sort(key=lambda x: x["_score"], reverse=True)
for c in scored[:5]:
    print(f"  score={c['_score']:.3f}  semantic={c['_metadata']['semantic_sim']:.3f}  cat_match={c['_metadata']['category_match']}  {c['title']}")

print()
print(f"groq_api_key set: {bool(config.groq_api_key)}")

print()
print("--- LLM filter stage ---")
from news_agent.models import TrendTopic
topic = TrendTopic(keyword="Messi", traffic=None, source="test")
final = retrieval._llm_reasoning_filter(topic, scored[:8], limit=4)
print(f"Final links after LLM filter: {len(final)}")
for f in final:
    print(f"  {f['title']}  — reason: {f['reason']}")

# diagnose_groq.py — isolates just the LLM filter call
from dotenv import load_dotenv
load_dotenv()
import sys
sys.path.insert(0, '.')

from groq import Groq
from news_agent.core.config import AppConfig

config = AppConfig.from_env()
client = Groq(api_key=config.groq_api_key)

candidates_text = """1. Title: World Cup 2026 Preview: USMNT's Starting XI
   Summary: ...
2. Title: Peru Elections 2026 Results: Statistical Tie Unfolds
   Summary: ...
3. Title: Belgium Vs Egypt: Red Devils Face Off
   Summary: ...
4. Title: Saudi Arabia Vs Uruguay: World Cup 2026 Match Ends in 1-1
   Summary: ..."""

prompt = f"""
Topic: Messi

Available Articles:
{candidates_text}

Select the top 4 relevant articles.
For each, provide:
1. "index": number from list
2. "relevant": boolean
3. "reason": a very short phrase (max 6 words) explaining the connection.

Return ONLY a valid JSON object with a "links" array of objects.
"""

response = client.chat.completions.create(
    model=config.groq_model,
    messages=[{"role": "user", "content": prompt}],
    temperature=0.1,
    response_format={"type": "json_object"},
    max_tokens=1024,
)
print(response.choices[0].message.content)