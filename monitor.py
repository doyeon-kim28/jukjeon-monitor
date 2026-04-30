"""
네이버 부동산 (fin.land.naver.com) 매물 추적기 (다중 지역 지원)
- 새 매물: 오늘 날짜로 등록된(confirmDate == 오늘) 매물
- 나간 매물: 이전 스냅샷에 있었는데 지금 없어진 매물
- 누적 DB로 전체 히스토리 관리
- HTML 대시보드 (탭 전환) + 이메일 알림
"""

import base64
import json
import os
import sys
import time
import smtplib
import shutil
import subprocess
import webbrowser
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.header import Header
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")
IS_CI = os.environ.get("CI") == "true"

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[오류] pip install playwright && python -m playwright install chromium")
    exit(1)

import config

# ── 지역 설정 ──
REGIONS = [
    {
        "id": "jukjeon",
        "name": "죽전동",
        "fullName": "경기도 용인시 수지구 죽전동",
        "sector": "죽전동",
        "bbox": {"left": 127.060, "right": 127.155, "top": 37.370, "bottom": 37.280},
    },
    {
        "id": "samjeon_sang",
        "name": "삼전동(상단)",
        "fullName": "송파구 삼전동 상단 민간도심복합개발 — 백제고분로 북측 빌라촌 (석촌호수 방면)",
        "sector": "삼전동",
        # 백제고분로 북측, 현대아파트·주상복합(127.106°E) 제외, 빌라/다가구 밀집구역
        "bbox": {"left": 127.088, "right": 127.101, "top": 37.511, "bottom": 37.504},
        "focus": "investment",
        "realEstateTypes": ["A05", "B01", "B02"],
        "typeCodeFilter": ["A05", "B01", "B02"],
    },
    {
        "id": "samjeon_ha",
        "name": "삼전동(하단)",
        "fullName": "송파구 삼전동 하단 민간도심복합개발 — 백제고분로28길 일대 (탄천 방면)",
        "sector": "삼전동",
        # 64-1번지 도심복합개발구역 중심(37.499°N, 127.093°E), 현대아파트(127.106°E) 제외
        "bbox": {"left": 127.088, "right": 127.101, "top": 37.504, "bottom": 37.496},
        "focus": "investment",
        "realEstateTypes": ["A05", "B01", "B02"],
        "typeCodeFilter": ["A05", "B01", "B02"],
    },
]

BASE_FILTER = {
    "tradeTypes": ["A1", "B1", "B2"],
    "realEstateTypes": ["A01", "A04", "A05", "B01", "B02"],
    "roomCount": [], "bathRoomCount": [], "optionTypes": [],
    "oneRoomShapeTypes": [], "moveInTypes": [],
    "filtersExclusiveSpace": False, "floorTypes": [],
    "directionTypes": [], "hasArticlePhoto": False,
    "isAuthorizedByOwner": False, "parkingTypes": [],
    "entranceTypes": [], "hasArticle": False,
}

TRADE_NAMES = {"A1": "매매", "B1": "전세", "B2": "월세"}
DATA_DIR = config.SNAPSHOT_DIR

JS_FETCH = """async (bodyStr) => {
    const res = await fetch("/front-api/v1/article/boundedArticles", {
        method: "POST",
        headers: {"Content-Type": "application/json", "Accept": "application/json"},
        body: bodyStr
    });
    return { status: res.status, body: await res.text() };
}"""


def won_to_str(won):
    if not won:
        return "-"
    man = won // 10000
    if man >= 10000:
        eok, rem = divmod(man, 10000)
        return f"{eok}억 {rem:,}" if rem else f"{eok}억"
    return f"{man:,}"


# ── 스크래핑 ──

TYPE_CODE_NAMES = {"A01": "아파트", "A04": "오피스텔", "A05": "단독/다가구", "B01": "빌라/연립", "B02": "원룸"}


def _parse_article(a, sector, type_code_filter=None):
    """API 응답 항목 1개를 파싱. 지정 sector/유형이 아니면 None."""
    rep = a.get("representativeArticleInfo", {})
    addr = rep.get("address", {})
    if addr.get("sector") != sector:
        return None
    type_code = rep.get("realEstateType", "") or ""
    if type_code_filter and type_code not in type_code_filter:
        return None
    type_name = TYPE_CODE_NAMES.get(type_code, type_code)

    aid = str(a.get("articleId", "") or rep.get("articleNumber", ""))
    if not aid:
        return None

    pi = rep.get("priceInfo", {})
    det = rep.get("articleDetail", {})
    sp = rep.get("spaceInfo", {})
    vi = rep.get("verificationInfo", {})
    trade_code = a.get("tradeType", rep.get("tradeType", ""))

    return aid, {
        "articleId": aid,
        "tradeType": trade_code,
        "tradeTypeName": TRADE_NAMES.get(trade_code, trade_code),
        "realEstateTypeName": type_name,
        "complexName": rep.get("complexName", a.get("articleName", "")),
        "dongName": rep.get("dongName", ""),
        "dealPrice": pi.get("dealPrice", 0) or 0,
        "warrantyPrice": pi.get("warrantyPrice", 0) or 0,
        "rentPrice": pi.get("rentPrice", 0) or 0,
        "dealStr": won_to_str(pi.get("dealPrice", 0) or 0),
        "warrantyStr": won_to_str(pi.get("warrantyPrice", 0) or 0),
        "rentStr": won_to_str(pi.get("rentPrice", 0) or 0),
        "exclusiveArea": sp.get("exclusiveSpace", 0),
        "supplySpaceName": sp.get("supplySpaceName", ""),
        "floorInfo": det.get("floorInfo", ""),
        "direction": det.get("direction", ""),
        "description": det.get("articleFeatureDescription", ""),
        "confirmDate": vi.get("articleConfirmDate", ""),
    }


def fetch_by_trade_type(page, trade_type, region):
    """특정 거래유형의 매물 전체 조회"""
    articles = {}
    last_info = []
    page_no = 0
    filt = dict(BASE_FILTER)
    filt["tradeTypes"] = [trade_type]
    if region.get("realEstateTypes"):
        filt["realEstateTypes"] = region["realEstateTypes"]
    sector = region["sector"]
    type_code_filter = region.get("typeCodeFilter")

    while True:
        body = {
            "filter": filt,
            "boundingBox": region["bbox"],
            "precision": 15,
            "userChannelType": "PC",
            "articlePagingRequest": {"size": 20, "sort": "RECENT", "lastInfo": last_info},
        }

        result = page.evaluate(JS_FETCH, json.dumps(body))
        if result["status"] != 200:
            break

        data = json.loads(result["body"])
        r = data.get("result", {})
        items = r.get("list", [])
        has_next = r.get("hasNextPage", False)
        last_info = r.get("lastInfo", [])

        if not items:
            break

        for a in items:
            parsed = _parse_article(a, sector, type_code_filter)
            if parsed:
                aid, article = parsed
                if aid not in articles:
                    articles[aid] = article

        page_no += 1
        if not has_next or page_no >= 50:
            break
        time.sleep(0.5)

    return articles


def run_scraping():
    """모든 지역 스크래핑. {region_id: {articleId: article}} 반환"""
    results = {}
    launch_args = ["--disable-blink-features=AutomationControlled"]
    if IS_CI:
        launch_args += ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=launch_args,
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="ko-KR",
            viewport={"width": 1280, "height": 900},
        )
        ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        page = ctx.new_page()
        print("  브라우저 접속 중...")
        for attempt in range(3):
            try:
                page.goto("https://fin.land.naver.com/", timeout=60000, wait_until="domcontentloaded")
                break
            except Exception as e:
                if attempt == 2:
                    raise
                print(f"  접속 재시도 ({attempt+1}/3): {e}")
                time.sleep(5)
        page.wait_for_timeout(2000)

        for region in REGIONS:
            print(f"\n  === {region['name']} ===")
            all_articles = {}
            for trade_type in ["A1", "B1", "B2"]:
                label = TRADE_NAMES.get(trade_type, trade_type)
                articles = fetch_by_trade_type(page, trade_type, region)
                all_articles.update(articles)
                print(f"  [{label}] {len(articles)}개")
                time.sleep(0.5)
            results[region["id"]] = all_articles

        browser.close()
    return results


# ── 스냅샷 / DB (지역별) ──

def snapshot_path(region_id):
    return os.path.join(DATA_DIR, f"latest_snapshot_{region_id}.json")


def db_path(region_id):
    return os.path.join(DATA_DIR, f"all_known_{region_id}.json")


def _migrate_legacy_files():
    """기존 단일 지역(죽전동) 파일을 지역별 파일로 이관"""
    legacy_snap = os.path.join(DATA_DIR, "latest_snapshot.json")
    legacy_db = os.path.join(DATA_DIR, "all_known.json")
    new_snap = snapshot_path("jukjeon")
    new_db = db_path("jukjeon")
    if os.path.exists(legacy_snap) and not os.path.exists(new_snap):
        shutil.move(legacy_snap, new_snap)
    if os.path.exists(legacy_db) and not os.path.exists(new_db):
        shutil.move(legacy_db, new_db)


def load_previous(region_id):
    p = snapshot_path(region_id)
    if not os.path.exists(p):
        return None, None
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("articles", {}), data.get("timestamp", "")


def save_snapshot(region_id, articles):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(snapshot_path(region_id), "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(articles),
            "articles": articles,
        }, f, ensure_ascii=False, indent=2)


def load_db(region_id):
    os.makedirs(DATA_DIR, exist_ok=True)
    p = db_path(region_id)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_db(region_id, db):
    with open(db_path(region_id), "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


def update_db(db, current_articles, gone_articles):
    """누적 DB 업데이트 (히스토리 기록용)"""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for aid, article in current_articles.items():
        if aid not in db:
            db[aid] = dict(article)
            db[aid]["firstSeen"] = now_str
            db[aid]["status"] = "active"
        else:
            db[aid].update(article)
            if db[aid].get("status") == "gone":
                db[aid]["status"] = "active"  # 재등록
        db[aid]["lastSeen"] = now_str

    for aid, article in gone_articles.items():
        if aid in db:
            db[aid]["status"] = "gone"
            db[aid]["goneDate"] = now_str


# ── 분석 ──

def analyze(current, prev_snapshot, today_str):
    """
    새 매물: confirmDate가 오늘인 것
    나간 매물: 이전 스냅샷에 있었는데 지금 없는 것
    """
    # 새 매물 = 오늘 등록된 것
    new_today = {
        aid: a for aid, a in current.items()
        if a.get("confirmDate", "") == today_str
    }

    # 나간 매물 = 이전 실행에 있었는데 지금 없는 것
    gone = {}
    if prev_snapshot:
        for aid in set(prev_snapshot) - set(current):
            gone[aid] = prev_snapshot[aid]

    return new_today, gone


# ── HTML 대시보드 ──

def load_image_base64(filename):
    """이미지를 base64로 인코딩 (HTML 내장용)"""
    paths = [
        os.path.join(DATA_DIR, filename),
        os.path.join(os.path.dirname(__file__), filename),
    ]
    for p in paths:
        if os.path.exists(p):
            with open(p, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")
    return ""


def _make_rows(items, cls=""):
    rows = []
    for a in sorted(items.values(), key=lambda x: (-(x.get("dealPrice", 0) or x.get("warrantyPrice", 0) or 0), x.get("complexName", ""))):
        c = f' class="{cls}"' if cls else ""
        if a.get("dealPrice"):
            price = a.get("dealStr", "-")
        elif a.get("rentPrice"):
            price = f'{a.get("warrantyStr", "-")} / {a.get("rentStr", "-")}'
        else:
            price = a.get("warrantyStr", "-")
        area = a.get("supplySpaceName") or (f'{a["exclusiveArea"]:.0f}㎡' if a.get("exclusiveArea") else "")
        extra = ""
        if a.get("goneDate"):
            extra = f'<br><small style="color:#c62828">나간 날: {a["goneDate"][:10]}</small>'
        rows.append(f"""<tr{c}>
            <td>{a.get('tradeTypeName','')}</td>
            <td><strong>{a.get('complexName','')}</strong></td>
            <td>{a.get('dongName','')}</td>
            <td class="price">{price}</td>
            <td>{area}</td>
            <td>{a.get('floorInfo','')}</td>
            <td>{a.get('confirmDate','')}{extra}</td>
            <td class="desc">{a.get('description','')}</td>
        </tr>""")
    return "\n".join(rows)


def _render_region_panel(region, data, today_str):
    """한 지역의 탭 패널 HTML 생성"""
    current = data["current"]
    new_today = data["new_today"]
    gone = data["gone"]
    all_gone_history = data["gone_history"]
    prev_time = data["prev_time"]

    maemae = {k: v for k, v in current.items() if v.get("tradeType") == "A1"}
    jeonse = {k: v for k, v in current.items() if v.get("tradeType") == "B1"}
    wolse = {k: v for k, v in current.items() if v.get("tradeType") == "B2"}

    gone_section = ""
    if gone:
        gone_section = f"""
        <div class="section">
            <h2>🔴 나간 매물 — {len(gone)}개</h2>
            <p class="sub">{prev_time} 스냅샷에 있었는데 지금({today_str}) 사라진 매물 (계약 완료 추정)</p>
            <table>
                <thead><tr><th>유형</th><th>단지</th><th>동</th><th>가격(만원)</th><th>면적</th><th>층</th><th>등록일</th><th>설명</th></tr></thead>
                <tbody>{_make_rows(gone, "gone")}</tbody>
            </table>
        </div>"""

    new_section = ""
    if new_today:
        new_section = f"""
        <div class="section">
            <h2>🟢 오늘의 새 매물 — {len(new_today)}개</h2>
            <p class="sub">오늘({today_str}) 네이버에 등록된 매물</p>
            <table>
                <thead><tr><th>유형</th><th>단지</th><th>동</th><th>가격(만원)</th><th>면적</th><th>층</th><th>등록일</th><th>설명</th></tr></thead>
                <tbody>{_make_rows(new_today, "new")}</tbody>
            </table>
        </div>"""

    no_change = ""
    if not gone and not new_today and prev_time:
        no_change = '<div class="section"><h2>변동 없음</h2><p>이전 대비 나간 매물도, 오늘 등록된 새 매물도 없습니다.</p></div>'

    first_run = ""
    if not prev_time:
        first_run = '<div class="section"><h2>첫 실행</h2><p>현재 매물을 기준점으로 저장했습니다. 다음 실행부터 변동을 추적합니다.</p></div>'

    gone_history = ""
    if all_gone_history:
        gone_history = f"""
        <div class="section">
            <h2>📋 나간 매물 누적 히스토리 — {len(all_gone_history)}개</h2>
            <p class="sub">모니터링 시작 이후 사라진 모든 매물</p>
            <table>
                <thead><tr><th>유형</th><th>단지</th><th>동</th><th>가격(만원)</th><th>면적</th><th>층</th><th>등록일 / 나간 날</th><th>설명</th></tr></thead>
                <tbody>{_make_rows(all_gone_history, "gone")}</tbody>
            </table>
        </div>"""

    complex_counts = Counter()
    for a in current.values():
        complex_counts[a.get("complexName", "")] += 1
    complex_rows = "\n".join(
        f'<tr><td>{name}</td><td>{cnt}개</td></tr>'
        for name, cnt in complex_counts.most_common(20)
    )

    is_investment = region.get("focus") == "investment"

    if is_investment:
        # 현재 매물 + 나간 매물 히스토리 모두 포함해서 가격 계산
        all_maemae = {**maemae, **{k: v for k, v in all_gone_history.items() if v.get("tradeType") == "A1"}}
        deal_prices = [a["dealPrice"] for a in all_maemae.values() if a.get("dealPrice")]
        avg_p = won_to_str(sum(deal_prices) // len(deal_prices)) if deal_prices else "-"
        min_p = won_to_str(min(deal_prices)) if deal_prices else "-"
        max_p = won_to_str(max(deal_prices)) if deal_prices else "-"
        gone_maemae = sum(1 for a in all_gone_history.values() if a.get("tradeType") == "A1")
        stats_html = f"""
        <div class="stats">
            <div class="stat-card maemae"><div class="number">{len(maemae)}</div><div class="label">현재 매물</div></div>
            <div class="stat-card total"><div class="number">{avg_p}</div><div class="label">평균 매매가</div></div>
            <div class="stat-card new"><div class="number">{min_p}</div><div class="label">최저 매매가</div></div>
            <div class="stat-card wolse"><div class="number">{max_p}</div><div class="label">최고 매매가</div></div>
            <div class="stat-card gone"><div class="number">{len(gone)}</div><div class="label">최근 거래(나간)</div></div>
            <div class="stat-card history"><div class="number">{gone_maemae}</div><div class="label">누적 매매 거래</div></div>
        </div>"""
        info_extra = "<p class='sub' style='color:#1565c0;font-weight:600;margin-top:6px'>💼 투자 추이 모니터링 — 매매가 변동 / 거래 발생 추적</p>"
    else:
        stats_html = f"""
        <div class="stats">
            <div class="stat-card total"><div class="number">{len(current)}</div><div class="label">현재 매물</div></div>
            <div class="stat-card maemae"><div class="number">{len(maemae)}</div><div class="label">매매</div></div>
            <div class="stat-card jeonse"><div class="number">{len(jeonse)}</div><div class="label">전세</div></div>
            <div class="stat-card wolse"><div class="number">{len(wolse)}</div><div class="label">월세</div></div>
            <div class="stat-card new"><div class="number">{len(new_today)}</div><div class="label">오늘 새 매물</div></div>
            <div class="stat-card gone"><div class="number">{len(gone)}</div><div class="label">나간 매물</div></div>
            <div class="stat-card history"><div class="number">{len(all_gone_history)}</div><div class="label">누적 나간</div></div>
        </div>"""
        info_extra = "<p class='sub' style='color:#2e7d32;font-weight:600;margin-top:6px'>🏠 보금자리 찾기 — 새 매물 / 나간 매물 추적</p>"

    filter_bar = f"""
    <div class="filter-bar">
        <span class="filter-label">거래 유형</span>
        <select class="trade-filter" onchange="filterTrade(this, '{region['id']}')">
            <option value="">전체</option>
            <option value="매매">매매</option>
            <option value="전세">전세</option>
            <option value="월세">월세</option>
        </select>
        <span class="filter-count" id="filter-count-{region['id']}"></span>
    </div>"""

    return f"""
    <div class="region-info">
        <h2>📍 {region['fullName']}</h2>
        <p class="sub">{prev_time or '첫 실행'} → 현재 | 네이버 부동산 기준</p>
        {info_extra}
    </div>
    {stats_html}
    {filter_bar}
    {first_run}
    {gone_section}
    {new_section}
    {no_change}
    {gone_history}
    <div class="two-col">
        <div class="section">
            <h2>전체 매물 목록</h2>
            <table>
                <thead><tr><th>유형</th><th>단지</th><th>동</th><th>가격(만원)</th><th>면적</th><th>층</th><th>등록일</th><th>설명</th></tr></thead>
                <tbody>{_make_rows(current)}</tbody>
            </table>
        </div>
        <div class="section">
            <h2>단지별 매물 수</h2>
            <table class="complex-table">
                <thead><tr><th>단지명</th><th>매물</th></tr></thead>
                <tbody>{complex_rows}</tbody>
            </table>
        </div>
    </div>
    """


def generate_html(region_data_map, today_str):
    """
    region_data_map: {region_id: {"region": region_cfg, "current": ..., "new_today": ..., "gone": ..., "gone_history": ..., "prev_time": ...}}
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    jjuni_sit_b64 = load_image_base64("jjuni_sit.png")
    jjuni_wave_b64 = load_image_base64("jjuni_wave.png")
    jjuni_sit_src = f"data:image/png;base64,{jjuni_sit_b64}" if jjuni_sit_b64 else ""
    jjuni_wave_src = f"data:image/png;base64,{jjuni_wave_b64}" if jjuni_wave_b64 else ""

    # 탭 버튼 + 패널
    tab_buttons = []
    tab_panels = []
    for idx, region in enumerate(REGIONS):
        rid = region["id"]
        if rid not in region_data_map:
            continue
        data = region_data_map[rid]
        active = " active" if idx == 0 else ""
        count = len(data["current"])
        tab_buttons.append(
            f'<button class="tab-btn{active}" data-tab="{rid}">{region["name"]} <span class="tab-count">{count}</span></button>'
        )
        panel_html = _render_region_panel(region, data, today_str)
        tab_panels.append(f'<div class="tab-panel{active}" id="panel-{rid}">{panel_html}</div>')

    html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>쭌이네 집 찾기 — {today_str}</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: 'Malgun Gothic', -apple-system, sans-serif; background: #faf8f5; color: #333; }}
    .header {{
        background: linear-gradient(135deg, #f8c471, #f0a04b);
        color: white; padding: 28px 40px; text-align: center;
        position: relative; overflow: hidden;
    }}
    .header .jjuni-left {{ position: absolute; left: 30px; bottom: 0; width: 90px; height: 90px; object-fit: contain; }}
    .header .jjuni-right {{ position: absolute; right: 30px; bottom: 0; width: 90px; height: 90px; object-fit: contain; }}
    .header h1 {{ font-size: 26px; margin-bottom: 6px; text-shadow: 0 1px 3px rgba(0,0,0,0.15); }}
    .header .sub {{ opacity: 0.9; font-size: 13px; }}
    .header .family {{ font-size: 15px; margin-top: 4px; opacity: 0.95; }}

    .tabs {{
        display: flex; justify-content: center; gap: 8px;
        background: white; padding: 14px 20px 0 20px;
        border-bottom: 1px solid #f0e0d0;
    }}
    .tab-btn {{
        padding: 12px 28px; font-size: 15px; font-weight: 600;
        border: none; background: #fdf6ee; color: #8a6e50;
        border-radius: 12px 12px 0 0; cursor: pointer;
        border-bottom: 3px solid transparent;
        transition: all 0.15s;
    }}
    .tab-btn:hover {{ background: #fce8d0; }}
    .tab-btn.active {{ background: #f0a04b; color: white; border-bottom-color: #d97e20; }}
    .tab-count {{
        display: inline-block; margin-left: 6px; padding: 2px 8px;
        background: rgba(255,255,255,0.3); border-radius: 10px; font-size: 12px;
    }}
    .tab-btn:not(.active) .tab-count {{ background: #e8d5bc; color: #8a6e50; }}
    .tab-panel {{ display: none; }}
    .tab-panel.active {{ display: block; }}

    .region-info {{ background: white; padding: 20px 40px; border-bottom: 1px solid #f0e0d0; }}
    .region-info h2 {{ font-size: 18px; color: #d97e20; }}
    .region-info .sub {{ color: #888; font-size: 12px; margin-top: 4px; }}

    .stats {{
        display: flex; justify-content: center; gap: 16px; flex-wrap: wrap;
        padding: 20px 40px; background: white;
        border-bottom: 1px solid #f0e0d0;
    }}
    .stat-card {{ text-align: center; padding: 15px 25px; border-radius: 12px; min-width: 120px; }}
    .stat-card.total {{ background: #fef3e2; }}
    .stat-card.maemae {{ background: #e3f2fd; }}
    .stat-card.maemae .number {{ color: #1565c0; }}
    .stat-card.jeonse {{ background: #e8f5e9; }}
    .stat-card.wolse {{ background: #fff3e0; }}
    .stat-card.gone {{ background: #ffebee; }}
    .stat-card.new {{ background: #e8f5e9; }}
    .stat-card.history {{ background: #f3e5f5; }}
    .stat-card .number {{ font-size: 32px; font-weight: bold; }}
    .stat-card .label {{ font-size: 12px; color: #666; margin-top: 4px; }}
    .stat-card.total .number {{ color: #e67e22; }}
    .stat-card.jeonse .number {{ color: #2e7d32; }}
    .stat-card.wolse .number {{ color: #e65100; }}
    .stat-card.gone .number {{ color: #c62828; }}
    .stat-card.new .number {{ color: #2e7d32; }}
    .stat-card.history .number {{ color: #7b1fa2; }}
    .tab-panel > .two-col, .tab-panel > .section {{ max-width: 1400px; margin: 20px auto; padding: 0 20px; }}
    .section {{ background: white; border-radius: 12px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
    .section h2 {{ font-size: 20px; margin-bottom: 12px; }}
    .section .sub {{ color: #888; font-size: 13px; margin-bottom: 16px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th {{ background: #fdf6ee; padding: 10px 12px; text-align: left; border-bottom: 2px solid #f0dcc8; font-weight: 600; white-space: nowrap; }}
    td {{ padding: 9px 12px; border-bottom: 1px solid #f5f0eb; }}
    tr:hover {{ background: #fdf8f3; }}
    tr.gone {{ background: #fff5f5; }}
    tr.gone:hover {{ background: #ffecec; }}
    tr.new {{ background: #f0fff0; }}
    tr.new:hover {{ background: #e0ffe0; }}
    .price {{ font-weight: bold; color: #d35400; white-space: nowrap; }}
    .desc {{ color: #888; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
    .two-col {{ display: grid; grid-template-columns: 1fr 300px; gap: 20px; }}
    .complex-table td {{ padding: 6px 10px; }}
    .filter-bar {{
        display: flex; align-items: center; gap: 14px;
        background: white; padding: 14px 40px;
        border-bottom: 1px solid #f0e0d0;
    }}
    .filter-label {{ font-size: 14px; font-weight: 600; color: #8a6e50; }}
    .trade-filter {{
        appearance: none; -webkit-appearance: none;
        padding: 9px 42px 9px 18px; font-size: 14px; font-weight: 600;
        border: 2px solid #f0a04b; border-radius: 22px;
        background-color: white;
        background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 24 24'%3E%3Cpath fill='%23f0a04b' d='M7 10l5 5 5-5z'/%3E%3C/svg%3E");
        background-repeat: no-repeat; background-position: right 14px center;
        color: #333; cursor: pointer; outline: none;
    }}
    .trade-filter:hover {{ border-color: #d97e20; background-color: #fef9f4; }}
    .trade-filter:focus {{ border-color: #d97e20; box-shadow: 0 0 0 3px rgba(240,160,75,0.2); }}
    .filter-count {{ font-size: 13px; color: #d97e20; font-weight: 600; }}
    .run-info {{ text-align: center; padding: 12px; color: #bbb; font-size: 11px; }}
    @media (max-width: 1000px) {{
        .two-col {{ grid-template-columns: 1fr; }}
        .header .jjuni-left, .header .jjuni-right {{ width: 60px; height: 60px; }}
        .filter-bar {{ padding: 12px 20px; }}
    }}
</style>
</head>
<body>
<div class="header">
    {"<img class='jjuni-left' src='" + jjuni_sit_src + "' alt='쭌이'>" if jjuni_sit_src else ""}
    {"<img class='jjuni-right' src='" + jjuni_wave_src + "' alt='쭌이'>" if jjuni_wave_src else ""}
    <h1>쭌이네 집 찾기 대시보드</h1>
    <div class="family">우리 가족의 보금자리 찾기 프로젝트</div>
    <div class="sub">{today_str} | {now_str} 업데이트</div>
</div>

<div class="tabs">
    {''.join(tab_buttons)}
</div>

{''.join(tab_panels)}

<div class="run-info">쭌이가 지켜보고 있어요! | {now_str} | 네이버 부동산 기준</div>

<script>
document.querySelectorAll('.tab-btn').forEach(btn => {{
    btn.addEventListener('click', () => {{
        const tab = btn.dataset.tab;
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById('panel-' + tab).classList.add('active');
    }});
}});

function filterTrade(sel, regionId) {{
    const val = sel.value;
    const panel = document.getElementById('panel-' + regionId);
    const rows = panel.querySelectorAll('table:not(.complex-table) tbody tr');
    let visible = 0;
    rows.forEach(tr => {{
        const type = tr.cells[0] ? tr.cells[0].textContent.trim() : '';
        const show = !val || type === val;
        tr.style.display = show ? '' : 'none';
        if (show) visible++;
    }});
    const countEl = document.getElementById('filter-count-' + regionId);
    if (countEl) {{
        countEl.textContent = val ? visible + '개' : '';
    }}
}}
</script>
</body>
</html>"""

    output_path = os.path.join(DATA_DIR, "dashboard.html")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path


# ── GitHub Pages 배포 ──

GITHUB_REPO_DIR = os.path.join(DATA_DIR)  # snapshots/ 가 git repo


def deploy_to_github(html_path):
    """대시보드를 GitHub Pages에 자동 배포"""
    try:
        index_path = os.path.join(DATA_DIR, "index.html")
        shutil.copy2(html_path, index_path)

        subprocess.run(
            ["git", "add", "index.html"],
            cwd=DATA_DIR, capture_output=True, text=True
        )
        # 변경 확인
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=DATA_DIR, capture_output=True
        )
        if diff.returncode == 0:
            print("  GitHub: 변경 없음 (배포 스킵)")
            return

        subprocess.run(
            ["git", "commit", "-m", f"update {datetime.now().strftime('%Y-%m-%d %H:%M')}"],
            cwd=DATA_DIR, capture_output=True, text=True
        )
        push = subprocess.run(
            ["git", "push"],
            cwd=DATA_DIR, capture_output=True, text=True, timeout=30
        )
        if push.returncode == 0:
            print("  GitHub Pages 배포 완료!")
            print("  https://doyeon-kim28.github.io/jukjeon-monitor/")
        else:
            print(f"  [GitHub 오류] {push.stderr[:200]}")
    except Exception as e:
        print(f"  [GitHub 오류] {e}")


# ── 이메일 ──

def format_article_text(a):
    price = a.get("warrantyStr", "-")
    if a.get("rentPrice"):
        price = f'{a.get("warrantyStr", "-")}/{a.get("rentStr", "-")}'
    return f'[{a.get("tradeTypeName","")}] {a.get("complexName","")} | {price} | {a.get("supplySpaceName","") or ""} | {a.get("floorInfo","")} | {a.get("confirmDate","")}'


def send_email(region_data_map, today_str):
    changed_regions = [
        (rid, d) for rid, d in region_data_map.items()
        if (d["new_today"] or d["gone"]) and d["prev_time"]
    ]
    if not changed_regions:
        return

    region_names = ", ".join(d["region"]["name"] for _, d in changed_regions)
    subject = f"[{region_names}] {today_str} 매물 변동"

    lines = [f"매물 변동 알림 ({today_str})", ""]
    for rid, d in changed_regions:
        lines.append(f"=== {d['region']['name']} (현재 {len(d['current'])}개) ===")
        if d["gone"]:
            lines.append(f"■ 나간 매물 - {len(d['gone'])}개")
            for a in d["gone"].values():
                lines.append("  " + format_article_text(a))
        if d["new_today"]:
            lines.append(f"■ 오늘 새 매물 - {len(d['new_today'])}개")
            for a in d["new_today"].values():
                lines.append("  " + format_article_text(a))
        lines.append("")

    body = "\n".join(lines)

    try:
        msg = MIMEMultipart()
        msg["From"] = config.EMAIL_SENDER
        msg["To"] = config.EMAIL_RECEIVER
        msg["Subject"] = Header(subject, "utf-8")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP_SSL("smtp.naver.com", 465) as smtp:
            smtp.login(config.EMAIL_SENDER, config.EMAIL_PASSWORD)
            smtp.send_message(msg)
        print("  이메일 발송 완료!")
    except Exception as e:
        if "ascii" in str(e).lower() or "앱비밀번호" in config.EMAIL_PASSWORD:
            print("  [이메일 오류] config.py의 EMAIL_PASSWORD를 네이버 앱비밀번호로 설정하세요.")
        else:
            print(f"  [이메일 오류] {e}")


# ── 메인 ──

def main():
    today_str = datetime.now().strftime("%Y-%m-%d")

    print("=" * 50)
    print("  부동산 매물 모니터링 (다중 지역)")
    print(f"  {today_str} ({datetime.now().strftime('%H:%M:%S')})")
    print("=" * 50)

    _migrate_legacy_files()

    # 수집 (모든 지역 한 번에)
    print("\n[수집]")
    scraped = run_scraping()
    if not scraped:
        print("  매물 수집 실패.")
        return

    region_data_map = {}

    print("\n[분석]")
    for region in REGIONS:
        rid = region["id"]
        current = scraped.get(rid, {})

        prev_snapshot, prev_time = load_previous(rid)
        new_today, gone = analyze(current, prev_snapshot, today_str)

        print(f"  [{region['name']}] 이전 {len(prev_snapshot) if prev_snapshot else 0} → 현재 {len(current)} | 새 {len(new_today)} | 나간 {len(gone)}")

        if current:
            save_snapshot(rid, current)

        db = load_db(rid)
        update_db(db, current, gone)
        save_db(rid, db)
        gone_history = {k: v for k, v in db.items() if v.get("status") == "gone"}

        region_data_map[rid] = {
            "region": region,
            "current": current,
            "new_today": new_today,
            "gone": gone,
            "gone_history": gone_history,
            "prev_time": prev_time,
        }

    if not region_data_map:
        print("  분석 대상 없음")
        return

    # 대시보드
    print("\n[대시보드]")
    html_path = generate_html(region_data_map, today_str)
    abs_path = os.path.abspath(html_path)
    print(f"  {abs_path}")
    # index.html로도 복사 (GitHub Pages)
    try:
        shutil.copy2(html_path, os.path.join(DATA_DIR, "index.html"))
    except Exception as e:
        print(f"  [index.html 복사 오류] {e}")
    if not IS_CI:
        webbrowser.open(f"file:///{abs_path}")
        deploy_to_github(html_path)

    # 이메일
    has_changes = any(
        (d["new_today"] or d["gone"]) and d["prev_time"]
        for d in region_data_map.values()
    )
    if has_changes:
        print("\n[이메일]")
        send_email(region_data_map, today_str)

    print("\n완료!")


if __name__ == "__main__":
    main()
