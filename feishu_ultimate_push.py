#!/usr/bin/env python3
"""
问天气象站 飞书推送 v8.0 (问天22维+LLM深度分析)
================================
v8.0 (2026-09-06): 推送前调用DeepSeek进行多源数据LLM深度分析, 新增AI分析卡片段
v1.1.2 (2026-09-03): 加入 wentian 22维度签名, 修复NoneType.format错误
v7.0: MCP优化 (complexity 48→25, 修SQL列名, 移除except:pass)
"""
import sys, os, json, urllib.request, ssl, sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any

sys.path.insert(0, '/root/.hermes')
FUSION_DIR = '/root/data/fusion'
DB = '/root/data/ano_weather.db'
# v1.1.2: 问天数据导出 (C权威, Python只读)
WENTIAN_JSON = '/root/data/fusion/wentian_latest.json'
LAT, LON = 25.09917, 102.92667  # ⚠ 2026-09-07: 修正为长水机场真坐标(原25.0820导致数据偏差)
ALT = 2103  # 长水机场ZPPP真海拔 2103.5m
FEISHU_USER = os.environ.get('FEISHU_USER_ID', 'ou_52a5a07c6c4c825ccb530efe5befcc77')

# ── 网络 ──────────────────────────────────────────────────────────
def _ctx(insecure: bool = True) -> ssl.SSLContext:
    """SSL context: insecure=True用于外部API(Open-Meteo等), insecure=False用于飞书(需验证)"""
    c = ssl.create_default_context()
    if insecure:
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
    return c

def _fetch(url: str, timeout: int = 15, retries: int = 3, insecure: bool = True) -> Optional[bytes]:
    """统一网络抓取 - 带重试(5xx/超时指数退避), 最终失败返回None而不是 'ERR:..'字符串
    ⚠ 修复(2026-09-05): 旧版无重试, Open-Meteo一次503整条推送实况全变0"""
    import time as _t
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'curl/7.81.0'})
            with urllib.request.urlopen(req, timeout=timeout, context=_ctx(insecure)) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last_err = e
            # 4xx不重试(参数错), 5xx重试
            if e.code < 500:
                print(f'[_fetch] {url[:80]}... HTTP {e.code}, 不重试')
                return None
        except Exception as e:
            last_err = e
        if attempt < retries:
            backoff = 2 ** attempt
            print(f'[_fetch] 第{attempt}次失败({last_err}), {backoff}s后重试')
            _t.sleep(backoff)
    print(f'[_fetch] {url[:80]}... 重试{retries}次仍失败: {last_err}')
    return None

# ── WMO天气码→中文/图标 ────────────────────────────────────────
WMO_TEXT = {
    0: '晴', 1: '晴间少云', 2: '多云', 3: '阴',
    45: '雾', 48: '雾凇',
    51: '小雨', 53: '中雨', 55: '大雨', 56: '冻雨', 57: '强冻雨',
    61: '小雨', 63: '中雨', 65: '大雨', 66: '冻雨', 67: '强冻雨',
    71: '小雪', 73: '中雪', 75: '大雪', 77: '雪粒',
    80: '阵雨', 81: '强阵雨', 82: '暴阵雨',
    85: '阵雪', 86: '强阵雪',
    95: '雷暴', 96: '雷暴伴冰雹', 99: '强雷暴伴冰雹'
}

WMO_ICON = {
    0: '☀️', 1: '🌤', 2: '⛅', 3: '☁️',
    45: '🌫', 48: '🌫',
    51: '🌦', 53: '🌦', 55: '🌧', 56: '🌧', 57: '🌧',
    61: '🌧', 63: '🌧', 65: '⛈', 66: '🌧', 67: '⛈',
    71: '🌨', 73: '🌨', 75: '❄️', 77: '🌨',
    80: '🌦', 81: '⛈', 82: '⛈',
    85: '🌨', 86: '❄️',
    95: '⛈', 96: '⛈', 99: '⛈'
}

def wmo_text(code: int) -> str:
    return WMO_TEXT.get(code, f'未知({code})')

def wmo_icon(code: int) -> str:
    return WMO_ICON.get(code, '❓')

# ── 风向 ───────────────────────────────────────────────────────────
def wind_dir(deg: Optional[float]) -> str:
    if deg is None: return '?'
    dirs = ['北', '东北', '东', '东南', '南', '西南', '西', '西北']
    return dirs[int((deg + 22.5) // 45) % 8]

# ── 1. 拉Open-Meteo 7天预报 ──────────────────────────────────────
def fetch_openmeteo() -> Dict[str, Any]:
    url = (
        f'https://api.open-meteo.com/v1/forecast?'
        f'latitude={LAT}&longitude={LON}'
        f'&current=temperature_2m,relative_humidity_2m,dew_point_2m,apparent_temperature,'
        f'precipitation,weather_code,cloud_cover,surface_pressure,pressure_msl,'
        f'wind_speed_10m,wind_direction_10m,wind_gusts_10m,uv_index,visibility'
        f'&hourly=temperature_2m,relative_humidity_2m,precipitation_probability,'
        f'precipitation,cloud_cover,visibility,wind_speed_10m,wind_direction_10m,pressure_msl'
        f'&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,'
        f'precipitation_probability_max,precipitation_hours,wind_speed_10m_max,'
        f'wind_direction_10m_dominant,uv_index_max,sunrise,sunset'
        f'&timezone=Asia/Shanghai&forecast_days=7'
    )
    d = _fetch(url)
    if not d:
        return {}
    try:
        return json.loads(d)
    except json.JSONDecodeError as e:
        print(f'[fetch_openmeteo] JSON解析失败: {e}')
        return {}

# ── 2. 拉本地数据库数据 ───────────────────────────────────────────
def get_uno() -> Optional[Dict[str, Any]]:
    """⚠ 已修复: UNO表实际列是 t/h/p/pa, 不是 T_c/rh/p_sea"""
    try:
        with sqlite3.connect(DB) as c:
            row = c.execute(
                "SELECT ts, t, h, pa, lr, sw, wx, p, alt "
                "FROM ano_weather WHERE source='UNO_v2.0_bridge' ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {
            'ts': row[0], 'T': row[1], 'rh': row[2], 'p_sea': row[3],  # pa=海平面气压
            'storm': row[4], 'warn': row[5], 'wx': row[6],
            'p': row[7],       # 本地机柜气压
            'alt': row[8]      # 海拔
        }
    except sqlite3.Error as e:
        print(f'[get_uno] DB查询失败: {e}')
        return None

def get_gnss_from_db() -> Optional[Dict[str, Any]]:
    """v3.0: 直接从问天DB读GNSS+S4数据, 绕过JSON桥接层时序问题"""
    try:
        with sqlite3.connect('/root/data/wentian.db') as c:
            row = c.execute(
                "SELECT g.gps_sats, g.bds_sats, g.pdop, "
                "       i.s4_gps, i.s4_bds, i.activity "
                "FROM local_gnss g "
                "LEFT JOIN local_iono i ON i.ts = (SELECT MAX(ts) FROM local_iono) "
                "ORDER BY g.ts DESC LIMIT 1"
            ).fetchone()
        if not row or row[0] is None:
            return None
        return {
            'gps_sats': int(row[0]), 'bds_sats': int(row[1]), 'pdop': float(row[2]),
            's4_gps': float(row[3] or 0), 's4_bds': float(row[4] or 0),
            'activity': str(row[5] or 'N/A')
        }
    except sqlite3.Error as e:
        print(f'[get_gnss_from_db] 失败: {e}')
        return None

def get_weathernext_summary() -> Optional[List[Dict]]:
    """v3.0: 从weathernext_forecast.json读15天预报摘要"""
    try:
        d = json.load(open('/root/data/fusion/weathernext_forecast.json'))
        out = []
        for day, v in sorted(d['summary']['daily'].items()):
            out.append({
                'date': day,
                't_min': v.get('temp_min'),
                't_max': v.get('temp_max'),
                'precip': v.get('precip_sum_mm', 0),
                'pressure': v.get('pressure_avg_hpa')
            })
        return out if out else None
    except Exception as e:
        print(f'[get_weathernext_summary] 失败: {e}')
        return None

def _load_json(path: str, default: Any = None) -> Any:
    """统一JSON加载器 - 失败不抛, 返回默认值"""
    try:
        if not os.path.exists(path):
            return default
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f'[_load_json] {path} 失败: {e}')
        return default

def get_ult_fusion() -> Optional[Dict[str, Any]]:
    """从本地融合预测JSON读1h/3h/6h"""
    return _load_json(f'{FUSION_DIR}/ultimate_forecast.json')

def get_wentian() -> Optional[Dict[str, Any]]:
    """
    v1.1.2: 读问天C程序导出的最新22维度数据
    问天是C权威数据源, Python只读不写
    返回None表示问天未运行/JSON不存在
    """
    return _load_json(WENTIAN_JSON)

def get_chronos() -> Optional[Dict[str, Any]]:
    return _load_json(f'{FUSION_DIR}/chronos_result.json')

def get_kriging() -> Optional[Dict[str, Any]]:
    return _load_json(f'{FUSION_DIR}/kriging_result.json')

def get_alerts() -> List[str]:
    d = _load_json(f'{FUSION_DIR}/alert_state.json', {})
    return d.get('alerts', []) if isinstance(d, dict) else []

# ── 3. 生活指数(基于实况+预报) ────────────────────────────────
def calc_indices(cur: Dict, daily_today: Dict) -> Dict[str, str]:
    """中央气象台7大指数"""
    out = {}
    T = cur.get('temperature_2m')
    H = cur.get('relative_humidity_2m')
    wind = cur.get('wind_speed_10m')
    vis = cur.get('visibility')
    uv = cur.get('uv_index')
    Tmax = daily_today.get('temp_max')
    Tmin = daily_today.get('temp_min')

    # 1. 穿衣
    if Tmax is not None:
        if Tmax >= 28: out['穿衣'] = '短袖'
        elif Tmax >= 24: out['穿衣'] = '短袖+薄外套'
        elif Tmax >= 18: out['穿衣'] = '长袖+外套'
        elif Tmax >= 10: out['穿衣'] = '毛衣+夹克'
        else: out['穿衣'] = '厚外套/棉服'

    # 2. 紫外线
    if uv is not None:
        if uv <= 2: out['紫外线'] = '最弱'
        elif uv <= 5: out['紫外线'] = '弱'
        elif uv <= 7: out['紫外线'] = '中等'
        elif uv <= 10: out['紫外线'] = '强'
        else: out['紫外线'] = '极强'

    # 3. 洗车
    if daily_today.get('precip_sum', 0) >= 1: out['洗车'] = '不宜'
    elif daily_today.get('precip_prob', 0) >= 60: out['洗车'] = '较不宜'
    else: out['洗车'] = '适宜'

    # 4. 晨练
    if vis and vis < 1000: out['晨练'] = '不宜(能见度低)'
    elif wind and wind > 20: out['晨练'] = '较不宜(大风)'
    elif T is not None and (T < 5 or T > 28): out['晨练'] = '较不宜'
    else: out['晨练'] = '适宜'

    # 5. 感冒
    if Tmin is not None and Tmax is not None:
        diff = Tmax - Tmin
        if Tmin <= 0: out['感冒'] = '极易发'
        elif diff >= 15: out['感冒'] = '易发'
        elif diff >= 10: out['感冒'] = '较易发'
        else: out['感冒'] = '少发'

    # 6. 过敏
    if wind and H:
        if wind >= 15 and H >= 70: out['过敏'] = '较高'
        elif H >= 80: out['过敏'] = '较高'
        else: out['过敏'] = '中等'

    # 7. 钓鱼
    if wind and T:
        cloud = cur.get('cloud_cover')
        if cloud is not None:
            if wind <= 10 and cloud >= 30: out['钓鱼'] = '适宜'
            elif wind <= 15: out['钓鱼'] = '较适宜'
            else: out['钓鱼'] = '不宜'

    return out

# ── 4. 拼接飞书卡片 ─────────────────────────────────────────────
def _safe_float(v: Any, default: float = 0.0) -> float:
    """安全转float - 处理None/字符串/异常"""
    if v is None: return default
    try: return float(v)
    except (ValueError, TypeError): return default

def _safe_int(v: Any, default: int = 0) -> int:
    if v is None: return default
    try: return int(v)
    except (ValueError, TypeError): return default

def _header(now: datetime) -> List[str]:
    """L1: 卡片头 (标题/日期/时间)"""
    today = now.date()
    tomorrow = today + timedelta(days=1)
    wd_cn = ['一','二','三','四','五','六','日']
    L = [
        '━━━━━━━━━━━━━━━━━━━━',
        f'🛰️ 问天气象站 · 昆明长水机场',
        f'📅 {today.strftime("%Y年%m月%d日")} 星期{wd_cn[today.weekday()]}',
        f'⏰ 发布时间: {now.strftime("%H:%M")}',
        '━━━━━━━━━━━━━━━━━━━━'
    ]
    return L

def get_db_current() -> Optional[Dict[str, Any]]:
    """降级数据源: Open-Meteo完全不可用时, 用本地DB最近一条室外实况兜底
    ⚠ 修复(2026-09-05): 杜绝503时推送全0实况"""
    try:
        with sqlite3.connect(DB) as c:
            row = c.execute(
                "SELECT ts, temp_outdoor, humid_outdoor, dew_point, feels_like, "
                "weather_code, cloud_cover_pct, pressure_msl_hpa, wind_speed_kmh, "
                "wind_dir_deg, wind_gust_kmh, uv_index, visibility_m "
                "FROM outdoor_weather WHERE temp_outdoor IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return {
            'ts': row[0], 'temperature_2m': row[1], 'relative_humidity_2m': row[2],
            'dew_point_2m': row[3], 'apparent_temperature': row[4],
            'weather_code': row[5], 'cloud_cover': row[6], 'pressure_msl': row[7],
            'wind_speed_10m': row[8], 'wind_direction_10m': row[9],
            'wind_gusts_10m': row[10], 'uv_index': row[11], 'visibility': row[12],
        }
    except sqlite3.Error as e:
        print(f'[get_db_current] DB查询失败: {e}')
        return None

# ⚠ 2026-09-07: 中文天气→图标映射 (问天C程序输出)
WX_CN_ICON = {
    '晴': '☀️', '晴间少云': '🌤', '少云': '🌤', '多云': '⛅', '阴': '☁️',
    '雾': '🌫', '雾凇': '🌫',
    '小雨': '🌦', '中雨': '🌦', '大雨': '🌧', '暴雨': '⛈',
    '阵雨': '🌦', '强阵雨': '⛈', '暴阵雨': '⛈',
    '冻雨': '🌧', '小雪': '🌨', '中雪': '🌨', '大雪': '❄️',
    '雷暴': '⛈', '雷暴伴冰雹': '⛈', '强雷暴伴冰雹': '⛈',
}

def _section_current(cur: Dict, uno: Optional[Dict], wx_code: int,
                     wentian: Optional[Dict] = None,
                     stale_note: str = '') -> List[str]:
    """L2: 当前实况 (温度/湿度/风/气压)
       ⚠ 2026-09-07: 天气显示优先问天DB真实观测, Open-Meteo weather_code仅做fallback
    """
    T_cur = _safe_float(cur.get('temperature_2m'), 0)
    H_cur = _safe_float(cur.get('relative_humidity_2m'), 0)
    Td = _safe_float(cur.get('dew_point_2m'), 0)
    feel = _safe_float(cur.get('apparent_temperature'), 0)
    wind_s = _safe_float(cur.get('wind_speed_10m'), 0)
    wind_d = _safe_float(cur.get('wind_direction_10m'), 0)
    gust = _safe_float(cur.get('wind_gusts_10m'), 0)
    uv_now = _safe_float(cur.get('uv_index'), 0)
    vis = _safe_float(cur.get('visibility'), 0)
    cloud = _safe_float(cur.get('cloud_cover'), 0)

    # 气压三级fallback (Open-Meteo current → DB outdoor_weather → surface_pressure)
    P_msl = cur.get('pressure_msl')

    # v2.2: 如果Open-Meteo API返回全0, fallback到问天数据
    wentian_used = False
    if T_cur == 0 and wentian and wentian.get('data',{}).get('outdoor',{}).get('temperature'):
        od = wentian['data']['outdoor']
        T_cur = _safe_float(od.get('temperature'), 0)
        H_cur = _safe_float(od.get('humidity'), 0)
        # ⚠ 修复(2026-09-05): outdoor.wind_speed源头是OM wind_speed_10m,已是km/h,
        # 旧代码再乘3.6会虚报51.5km/h假风速
        wind_s = _safe_float(od.get('wind_speed'), 0)
        wind_d = _safe_float(od.get('wind_dir'), 0)
        cloud = _safe_float(od.get('cloud_cover'), 0)
        uv_now = _safe_float(od.get('uv'), 0)
        # ⚠ 修复(2026-09-05): 问天outdoor.pressure_msl实为站点压(≈803),
        # 不能直接当海平面气压显示; 气压统一走下面DB(>900过滤)链路
        feel = T_cur
        wentian_used = True
        print(f'[实况] Open-Meteo无数据, 问天fallback: T={T_cur}°C H={H_cur}%')

    if P_msl is None or _safe_float(P_msl) < 900:
        try:
            with sqlite3.connect(DB) as c:
                row = c.execute(
                    "SELECT pressure_msl_hpa FROM outdoor_weather "
                    "WHERE pressure_msl_hpa > 900 ORDER BY ts DESC LIMIT 1"
                ).fetchone()
            if row and row[0]: P_msl = row[0]
        except sqlite3.Error as e:
            print(f'[_section_current] 气压DB查询失败: {e}')
    if P_msl is None:
        P_msl = cur.get('surface_pressure', 0)

    # ⚠ 2026-09-07: 天气显示优先问天DB真实观测 (wentian.outdoor.weather),
    # Open-Meteo weather_code仅做fallback
    wx_label = wmo_text(wx_code)
    wx_icon = wmo_icon(wx_code)
    if wentian:
        od = wentian.get('data', {}).get('outdoor', {})
        wt = od.get('weather')
        if wt and isinstance(wt, str) and wt.strip():
            wt = wt.strip()
            wx_label = wt
            wx_icon = WX_CN_ICON.get(wt, '🌡')
            print(f'[实况] 使用问天DB天气: {wt}')

    L = [
        '',
        f'🌡 【当前实况】 {wx_icon} {wx_label}',
        f'  温度: {T_cur:.1f}°C (体感 {feel:.1f}°C)',
        f'  湿度: {H_cur:.0f}%  露点: {Td:.1f}°C',
        f'  风: {wind_dir(wind_d)}风 {wind_s:.1f}km/h (阵风{gust:.1f}km/h)',
        f'  气压: {_safe_float(P_msl):.1f}hPa  云量: {cloud:.0f}%',
        f'  UV: {uv_now:.1f}  能见度: {vis/1000:.1f}km'
    ]
    if uno:
        L.append(f'  📡 机柜实测: {_safe_float(uno.get("T"), 0):.1f}°C / 湿度{_safe_float(uno.get("rh"), 0):.0f}% / 海平面气压{_safe_float(uno.get("p_sea"), 0):.1f}hPa')
    if wentian_used or stale_note:
        src = stale_note or '本地DB缓存(Open-Meteo暂不可达)'
        L.append(f'  ⚠ 实况来源: {src} (非实时, 数据可能滞后)')
    return L

def _smart_wx(rain_total: float, max_cloud: float, code: int) -> tuple:
    """根据降水+云量+code智能选天气"""
    if rain_total >= 5: return ('🌧', '雨')
    if rain_total >= 1: return ('🌦', '阵雨')
    if code in (61, 63, 65): return ('🌧', wmo_text(code))
    if code in (95, 96, 99): return ('⛈', wmo_text(code))
    if code == 0: return ('☀️', '晴')
    if code in (1, 2): return ('⛅', '多云')
    if code == 3: return ('☁️', '阴')
    if code in (51, 53, 55): return ('🌦', '阵雨')
    if max_cloud >= 80: return ('☁️', '阴')
    if max_cloud >= 40: return ('⛅', '多云')
    return ('☀️', '晴')

def _split_day_night(hourly: Dict, today_str: str) -> Dict[str, List]:
    """L3 helper: 拆分今日白天/夜间数据"""
    result = {'day_temps': [], 'night_temps': [], 'day_rains': [], 'night_rains': [],
              'day_clouds': [], 'night_clouds': []}
    if not hourly.get('time'):
        return result
    for j, ht in enumerate(hourly['time']):
        if not ht.startswith(today_str):
            continue
        try:
            hr = int(ht.split('T')[1].split(':')[0])
            t = hourly['temperature_2m'][j]
            r = hourly['precipitation'][j] if j < len(hourly.get('precipitation', [])) else 0
            cl = hourly['cloud_cover'][j] if j < len(hourly.get('cloud_cover', [])) else 0
            key = 'day' if 8 <= hr < 20 else 'night'
            result[f'{key}_temps'].append(t)
            result[f'{key}_rains'].append(r)
            result[f'{key}_clouds'].append(cl)
        except (ValueError, IndexError) as e:
            print(f'[_split_day_night] 解析失败: {e}')
    return result

def _section_today(daily: Dict, hourly: Dict, today: datetime.date) -> List[str]:
    """L3: 今日白天/夜间天气预报"""
    L = []
    times = daily.get('time', [])
    if not times:
        return L

    i = 0  # 今日
    wx = daily['weather_code'][i]
    rain = _safe_float(daily['precipitation_sum'][i])
    rain_prob = _safe_int(daily['precipitation_probability_max'][i])
    wind_max = _safe_float(daily['wind_speed_10m_max'][i])
    wind_dom = _safe_float(daily['wind_direction_10m_dominant'][i])
    T_max = _safe_float(daily['temperature_2m_max'][i])
    T_min = _safe_float(daily['temperature_2m_min'][i])

    sr = (daily.get('sunrise', [None]*7)[i] or '--').split('T')[-1] if daily.get('sunrise') else '--:--'
    ss = (daily.get('sunset', [None]*7)[i] or '--').split('T')[-1] if daily.get('sunset') else '--:--'

    sn = _split_day_night(hourly, today.strftime('%Y-%m-%d'))
    day_ic, day_tx = _smart_wx(sum(sn['day_rains']), max(sn['day_clouds']) if sn['day_clouds'] else 0, wx)
    night_ic, night_tx = _smart_wx(sum(sn['night_rains']), max(sn['night_clouds']) if sn['night_clouds'] else 0, wx)

    L.extend([
        '',
        '━━━ 📅 今日天气预报 ━━━',
        f'  白天: {day_ic} {day_tx}  夜间: {night_ic} {night_tx}',
        f'  温度: {T_min:.0f}°C ~ {T_max:.0f}°C',
        f'  降水: {rain:.1f}mm  概率: {rain_prob}%',
        f'  风: {wind_dir(wind_dom)}风 {wind_max:.0f}km/h',
        f'  日出: {sr}  日落: {ss}'
    ])
    if sn['day_temps']:
        L.append(f'  白天详情: {day_ic} {day_tx} {min(sn["day_temps"]):.0f}~{max(sn["day_temps"]):.0f}°C')
    if sn['night_temps']:
        L.append(f'  夜间详情: {night_ic} {night_tx} {min(sn["night_temps"]):.0f}~{max(sn["night_temps"]):.0f}°C')
    return L

def _section_short_fusion(ult: Optional[Dict]) -> List[str]:
    """L4: 短期融合预测 (1h/3h/6h)"""
    if not ult or not ult.get('temperature'):
        return []
    t1 = ult['temperature'].get('1h')
    t3 = ult['temperature'].get('3h')
    t6 = ult['temperature'].get('6h')
    p1 = ult.get('pressure', {}).get('1h')
    conf = ult.get('temperature', {}).get('confidence', {})
    q10 = conf.get('temp_1h_q10')
    q90 = conf.get('temp_1h_q90')
    conf_str = f' (置信{q10:.0f}~{q90:.0f}°C)' if q10 and q90 else ''

    L = ['', '━━━ ⏱ 短期融合预测 ━━━']
    if t1 is not None:
        L.append(f'  1小时后: {t1:.1f}°C{conf_str} 气压{p1:.1f}hPa' if p1 else f'  1小时后: {t1:.1f}°C{conf_str}')
    if t3 is not None: L.append(f'  3小时后: {t3:.1f}°C')
    if t6 is not None: L.append(f'  6小时后: {t6:.1f}°C')
    return L

def _section_6day(daily: Dict) -> List[str]:
    """L5: 未来6天预报 (v3.0: 优先WeatherNext 64员ensemble, fallback Open-Meteo)"""
    L = ['', '━━━ 📆 未来6天预报 (WeatherNext 2) ━━━']
    
    # 优先读WeatherNext
    wn = get_weathernext_summary()
    if wn and len(wn) >= 2:
        wd_cn = ['一','二','三','四','五','六','日']
        for i in range(1, min(7, len(wn))):
            d = wn[i]
            try:
                dt = datetime.fromisoformat(d['date'])
                tmax = d['t_max'] or 0
                tmin = d['t_min'] or 0
                rain = d['precip'] or 0
                if rain >= 5: wx_text, wx_ic = '雨', '🌧'
                elif rain >= 1: wx_text, wx_ic = '阵雨', '🌦'
                else: wx_text, wx_ic = '多云', '⛅'
                L.append(f'  {dt.strftime("%m/%d")} 周{wd_cn[dt.weekday()]}: {wx_ic} {wx_text:<4} '
                         f'{tmin:.0f}~{tmax:.0f}°C  💧{rain:.1f}mm')
            except Exception as e:
                print(f'[_section_6day] WeatherNext解析跳过: {e}')
        return L
    
    # fallback: Open-Meteo daily
    L = ['', '━━━ 📆 未来6天预报 ━━━']
    times = daily.get('time', [])
    wd_cn = ['一','二','三','四','五','六','日']
    for i in range(1, 7):
        if i >= len(times): break
        d = datetime.fromisoformat(times[i])
        wx = daily['weather_code'][i]
        T_max = _safe_float(daily['temperature_2m_max'][i])
        T_min = _safe_float(daily['temperature_2m_min'][i])
        rain = _safe_float(daily['precipitation_sum'][i])
        prob = _safe_int(daily['precipitation_probability_max'][i])
        wind = _safe_float(daily['wind_speed_10m_max'][i])
        wd_dir = _safe_float(daily['wind_direction_10m_dominant'][i])

        # 智能天气选择 (中央气象台策略)
        if rain >= 5: wx_text, wx_ic = '雨', '🌧'
        elif rain >= 1: wx_text, wx_ic = '阵雨', '🌦'
        elif prob >= 40 and wx in (51, 53, 55, 80, 81, 82, 61, 63, 65): wx_text, wx_ic = '阵雨', '🌦'
        else: wx_text, wx_ic = wmo_text(wx), wmo_icon(wx)

        L.append(f'  {d.strftime("%m/%d")} 周{wd_cn[d.weekday()]}: {wx_ic} {wx_text:<4} '
                 f'{T_min:.0f}~{T_max:.0f}°C  💧{rain:.1f}mm({prob}%)  💨{wind_dir(wd_dir)}{wind:.0f}')
    return L

def _section_indices(cur: Dict, daily: Dict) -> List[str]:
    """L6: 中央气象台7大生活指数"""
    daily_today = {
        'temp_max': _safe_float(daily.get('temperature_2m_max', [0])[0]) if daily.get('temperature_2m_max') else 0,
        'temp_min': _safe_float(daily.get('temperature_2m_min', [0])[0]) if daily.get('temperature_2m_min') else 0,
        'precip_sum': _safe_float(daily.get('precipitation_sum', [0])[0]) if daily.get('precipitation_sum') else 0,
        'precip_prob': _safe_int(daily.get('precipitation_probability_max', [0])[0]) if daily.get('precipitation_probability_max') else 0
    }
    indices = calc_indices(cur, daily_today)
    if not indices:
        return []
    icon_map = {'穿衣':'👔', '紫外线':'☀️', '洗车':'🚗', '晨练':'🏃',
                '感冒':'🤧', '过敏':'🌿', '钓鱼':'🎣'}
    L = ['', '━━━ 🌈 生活指数 ━━━']
    for k, v in indices.items():
        L.append(f'  {icon_map.get(k, "•")} {k}: {v}')
    return L

def _section_analysis(wentian: Optional[Dict]) -> List[str]:
    """L4.5: 综合分析 · 模型结论 (问天v2.3 C引擎结论, 主人2026-09-04要求)"""
    if not wentian or not isinstance(wentian, dict):
        return []
    wd = wentian.get('data', {})
    if not isinstance(wd, dict):
        return []

    def _g(d, *keys, default=None):
        for k in keys:
            if not isinstance(d, dict):
                return default
            d = d.get(k)
        return d if d is not None else default

    L = ['', '━━━ 🧠 综合分析 · 模型结论 ━━━']
    shown = 0

    # 1. 多源融合预测结论 (模块21: 8通道投票)
    ms = wd.get('multi_source', {})
    if isinstance(ms, dict) and ms.get('final_weather'):
        lw = ms.get('level', 'NORMAL')
        lvl_ic = {'NORMAL': '✅', 'WATCH': '🟡', 'WARNING': '🟠', 'ALERT': '🔴'}.get(lw, '✅')
        L.append(f'  {lvl_ic} 8源投票: {ms["final_weather"]} | 战备={lw} 风暴分={_g(ms, "storm_score", default=0)}/5')
        parts = [f'投票源: Zambretti {ms.get("zambretti", "-")}',
                 f'OM3h {ms.get("openmeteo_3h", "-")}',
                 f'METAR {ms.get("metar_now", "-")}']
        L.append('  ' + ' | '.join(parts))
        shown += 1

    # 2. 短临Nowcasting结论 (模块17/18: GB/T 35663五天气型)
    nc = wd.get('nowcast', {})
    if isinstance(nc, dict) and nc.get('forecast'):
        wl = nc.get('warning_level', '无')
        ic = '✅' if wl in ('无', '', None) else '⚠️'
        L.append(f'  {ic} 短临Nowcast(0-30min): {nc["forecast"]} | 雷暴评分{_g(nc, "score", default=0)}/100 告警={wl}')
        pwv_v = _g(nc, "pwv_current", default=None)
        pwv_str = '不可用' if (pwv_v is None or pwv_v <= 0.5) else f'{pwv_v:.1f}mm'
        L.append(f'     五型评分 ⛈{_g(nc, "thunder_score", default=0)} 🌪{_g(nc, "squall_score", default=0)} '
                 f'🌫{_g(nc, "stationary_score", default=0)} 💨{_g(nc, "wind_shear_score", default=0)} '
                 f'| PWV {pwv_str}')
        shown += 1

    # 3. PWV反演 + 多源S4融合 (模块17/23)
    pwv = wd.get('pwv', {})
    m4 = wd.get('multisrc_s4', {})
    if isinstance(pwv, dict) and pwv.get('pwv_mm'):
        s4line = ''
        if isinstance(m4, dict) and _g(m4, 'fused_s4', default=0) > 0:
            s4line = (f' | 5源S4融合={m4["fused_s4"]:.3f}({m4.get("level", "?")}'
                      f' 置信{_g(m4, "confidence", default=0):.0%} 有效{_g(m4, "used_n", default=0)}/5源)')
        L.append(f'  💧 PWV反演: {pwv["pwv_mm"]:.1f}mm (Δ{_g(pwv, "delta_pwv", default=0):+.2f}mm){s4line}')
        shown += 1

    # 4. 自进化评分 (模块22)
    evo = wd.get('evolution', [])
    if isinstance(evo, list) and evo:
        seg = ' / '.join(f'{e.get("predictor", "?")}={_g(e, "score", default=0)}分'
                         f'(温MAE {_g(e, "mae_temp", default=0):.1f}°C 压MAE {_g(e, "mae_press", default=0):.1f}hPa)'
                         for e in evo[:3])
        L.append(f'  📈 自进化引擎: {seg}')
        shown += 1

    return L if shown else []


# ── 7. LLM深度分析 (v8.0) ────────────────────────────────────────
def _section_llm_analysis() -> List[str]:
    """L8: 调用系统默认LLM对多源数据深度分析, 展示AI级气象洞察
    读 /root/data/fusion/llm_enhanced_analysis.json
    """
    path = '/root/data/fusion/llm_enhanced_analysis.json'
    try:
        with open(path) as f:
            a = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f'[_section_llm_analysis] 读取失败: {e}')
        return []

    if not isinstance(a, dict) or a.get('parse_failed'):
        return []

    L = ['', '━━━ 🧠 AI深度分析 ━━━']

    # 1. 跨模型共识
    consensus = a.get('cross_model_consensus', {})
    temp = consensus.get('temperature', {})
    press = consensus.get('pressure', {})
    precip = consensus.get('precipitation', {})

    if temp.get('consensus_celsius') is not None:
        cons_t = temp['consensus_celsius']
        if isinstance(cons_t, (int, float)):
            rng = temp.get('range_celsius', 0)
            if isinstance(rng, (int, float)):
                models = '✅一致' if temp.get('models_agree') else '⚠有分歧'
                L.append(f'  🌡 温度共识: {cons_t:.1f}°C (跨度{rng:.1f}°C {models})')

    if press.get('consensus_hpa') is not None:
        p = press['consensus_hpa']
        if isinstance(p, (int, float)):
            L.append(f'  🏋 气压共识: {p:.1f}hPa')

    if precip.get('probability_percent') is not None:
        pp = precip['probability_percent']
        if isinstance(pp, (int, float)):
            L.append(f'  💧 降水概率: {pp}%')

    # 2. 异常检测
    anomaly = a.get('anomaly_detection', {})
    if anomaly.get('has_anomaly'):
        for ano in anomaly.get('anomalies', [])[:3]:
            L.append(f'  ⚠ {ano.get("source","?")}/{ano.get("field","?")}: {ano.get("value")} ({ano.get("severity","?")})')

    # 3. 置信度
    conf = a.get('confidence_assessment', {})
    ic = {'high': '🟢', 'medium': '🟡', 'low': '🔴'}
    overall = conf.get('overall_confidence', '?')
    short = conf.get('short_term_confidence', '?')
    long_ = conf.get('long_term_confidence', '?')
    L.append(f'  📊 置信度: 总体{ic.get(overall,"?")}{overall} 短临{ic.get(short,"?")}{short} 远期{ic.get(long_,"?")}{long_}')

    # 4. 钦天监增强
    q = a.get('qintianjian_enhancement', {})
    if q.get('overall_assessment'):
        L.append(f'  🧿 {q["overall_assessment"]}')
    if q.get('ancient_wisdom_note'):
        L.append(f'  📜 {q["ancient_wisdom_note"]}')

    # 5. 趋势
    trend = a.get('trend_analysis', {})
    tdir = trend.get('temperature_trend', '?')
    pdir = trend.get('pressure_trend', '?')
    tic = {'rising': '↗', 'stable': '→', 'falling': '↘'}
    L.append(f'  📈 短临趋势: 温{tic.get(tdir,"?")}{tdir} 压{tic.get(pdir,"?")}{pdir}')
    if trend.get('key_concern'):
        L.append(f'  ⚡ 关注: {trend["key_concern"]}')

    # 6. 告警建议
    alert_rec = a.get('alert_recommendation', {})
    if alert_rec.get('should_alert'):
        lvl = alert_rec.get('alert_level', 'none')
        ic2 = {'watch': '🟡', 'warning': '🟠', 'alert': '🔴'}
        L.append(f'  {ic2.get(lvl,"?")} 告警建议: {lvl} - {alert_rec.get("reason","")}')

    # 7. 自优化建议
    opt = a.get('self_optimization', {})
    if opt.get('suggested_weight_adjustments'):
        L.append(f'  🔧 自优化: {opt["suggested_weight_adjustments"][:60]}...')
    if opt.get('qintianjian_adjustment_suggestion'):
        L.append(f'  🔮 钦天监优化: {opt["qintianjian_adjustment_suggestion"][:60]}...')

    return L if len(L) > 1 else []


def _section_footer(ult: Optional[Dict], alerts: List[str],
                    wentian: Optional[Dict] = None) -> List[str]:
    """L7: 预警 + 数据签名 + URL (v1.1.2: 加入问天22维度签名)
    
    v1.1.2 修复: 所有wentian字段用_get安全取值, 防NoneType.format错误
    """
    def _get(d, *keys, default=None):
        """多层dict安全取值, 任意层为None返回default"""
        for k in keys:
            if not isinstance(d, dict):
                return default
            d = d.get(k)
        return d if d is not None else default
    
    L = ['']
    if alerts:
        L.append('━━━ ⚠️ 预警信息 ━━━')
        L.extend(f'  {a}' for a in alerts[:5])
    else:
        L.append('━━━ ✅ 预警信息 ━━━')
        L.append('  当前无气象/电离层预警')

    # 模型精度签名
    if ult:
        individual = ult.get('individual', {})
        t_mae = individual.get('chronos_temp_mae')
        p_mae = individual.get('chronos_pressure_mae')
        if t_mae is not None:
            L.append('')
            L.append(f'📊 模型精度: 温度MAE={t_mae:.2f}°C 气压MAE={p_mae:.2f}hPa')

    # v1.1.2: 问天22维度签名 (C权威, 所有字段None-safe)
    if wentian and isinstance(wentian, dict):
        wd = wentian.get('data', {})
        if not isinstance(wd, dict):
            wd = {}
        
        # 各字段None-safe (使用_get)
        kp = _get(wd, 'swpc', 'kp')
        kp_text = _get(wd, 'swpc', 'kp_text', default='?')
        flux = _get(wd, 'swpc_f107', 'flux_sfu', default=0)
        if kp is not None:
            L.append(f'  Kp={kp:.1f} ({kp_text}) | 太阳活动={flux:.0f} sfu')
        
        sc = wd.get('swpc_scale', {})
        if isinstance(sc, dict) and sc:
            L.append(f'  NOAA尺度: G{sc.get("g_scale",0)} '
                      f'S{sc.get("s_scale",0)} R{sc.get("r_scale",0)}')
        
        fused = _get(wd, 'fusion', 'fused_pressure')
        sigma = _get(wd, 'fusion', 'sigma', default=0)
        if fused is not None:
            L.append(f'  Kalman气压融合: {fused:.2f}hPa (σ={sigma:.2f})')
        
        pm25 = _get(wd, 'air_quality', 'pm25', default=0)
        aqi = _get(wd, 'air_quality', 'aqi')
        if aqi is not None:
            L.append(f'  空气质量: PM2.5={pm25:.1f}μg AQI={aqi}')

        # v4.0: 钦天监 · 节气+五行+卦象 (从imperial_enhancement.json读)
        _imp_path = '/root/data/fusion/imperial_enhancement.json'
        if os.path.exists(_imp_path):
            try:
                with open(_imp_path) as _f:
                    _imp = json.load(_f)
                _st = _imp.get('solar_term')
                _hx = _imp.get('hexagram')
                _wq = _imp.get('wuxing_quadrant')
                _note = _imp.get('enhancement_note')
                if _st or _note:
                    L.append('')
                    L.append(f'🧿 钦天监 · {_note or ""}')
                if _hx:
                    L.append(f'  ☰ 卦象: {_hx} | 系统: {"稳定" if _imp.get("system_stable") else "不稳定"}')
            except Exception:
                pass

        # v3.0: 直接从DB读GNSS/S4数据（问天权威源，绕过JSON桥接层时序问题）
        gnss_data = get_gnss_from_db()
        if gnss_data:
            gps_n = gnss_data['gps_sats']
            bds_n = gnss_data['bds_sats']
            pdop = gnss_data['pdop']
            s4g = gnss_data['s4_gps']
            s4b = gnss_data['s4_bds']
            act = gnss_data['activity']
            L.append(f'  电离层S4: GPS={s4g:.3f} 北斗={s4b:.3f} ({act})')
            L.append(f'  GNSS: {gps_n}颗GPS + {bds_n}颗北斗  DOP={pdop:.1f}')
        else:
            # fallback到问天JSON
            s4_gps = _get(wd, 'local_iono', 's4_gps')
            s4_bds = _get(wd, 'local_iono', 's4_bds', default=0)
            act = _get(wd, 'local_iono', 'activity', default='?')
            if s4_gps is not None:
                L.append(f'  电离层S4: GPS={s4_gps:.3f} 北斗={s4_bds:.3f} ({act})')
            gps_n = _get(wd, 'local_gnss', 'gps_sats', default=0)
            bds_n = _get(wd, 'local_gnss', 'bds_sats', default=0)
            pdop = _get(wd, 'local_gnss', 'pdop', default=0)
            if gps_n is not None or bds_n is not None:
                L.append(f'  GNSS: {gps_n}颗GPS + {bds_n}颗北斗  DOP={pdop:.1f}')

    L.extend([
        '',
        '━━━━━━━━━━━━━━━━━━━━',
        '📡 数据源: 问天v2.3 (17API+4硬件+Kalman+8源投票+自进化+星象+WeatherNext=38维)',
        '🔗 问天短临',
        '━━━━━━━━━━━━━━━━━━━━'
    ])
    return L

def build_message(om: Dict, uno: Optional[Dict], ult: Optional[Dict],
                  chronos: Any, kriging: Any, alerts: List[str],
                  wentian: Optional[Dict] = None) -> str:
    """
    🎯 问天气象站 飞书卡片构建器 (优化版)

    拆分前: complexity=48, lines=246 (单巨型函数)
    拆分后: 7个_section_* helper, 主函数50行

    v1.1.2: 加入 wentian 参数显示22维度问天数据
    """
    now = datetime.now()
    cur = om.get('current', {})
    daily = om.get('daily', {})
    hourly = om.get('hourly', {})

    # ⚠ 修复(2026-09-05): Open-Meteo重试后仍失败时, 用本地DB最新实况兜底,
    # 并明确标注"非实时" — 绝不再推送全0实况
    stale_note = ''
    if not cur.get('temperature_2m'):
        dbcur = get_db_current()
        if dbcur:
            stale_note = f'本地DB缓存 {dbcur["ts"]} (Open-Meteo暂不可达)'
            cur = dbcur
            print(f'[实况] Open-Meteo不可用, DB兜底: {stale_note}')
        else:
            stale_note = '无任何实时数据源 (Open-Meteo不可达且DB无缓存)'

    # 当前小时weather_code (从current获取)
    wx_code = _safe_int(cur.get('weather_code', 0))

    # 拼接7个段落
    L = []
    L += _header(now)
    L += _section_current(cur, uno, wx_code, wentian, stale_note)
    if daily.get('time'):
        L += _section_today(daily, hourly, now.date())
    L += _section_short_fusion(ult)
    L += _section_analysis(wentian)
    L += _section_llm_analysis()
    if daily.get('time'):
        L += _section_6day(daily)
        L += _section_indices(cur, daily)
    L += _section_footer(ult, alerts, wentian)

    return '\n'.join(L)

# ── 5. 发飞书 ───────────────────────────────────────────────────
def _get_feishu_secret() -> Optional[str]:
    try:
        with open('/root/.hermes/.env') as f:
            return f.read().split('FEISHU_APP_SECRET=')[1].split('\n')[0].strip()
    except (OSError, IndexError) as e:
        print(f'[_get_feishu_secret] 读取失败: {e}')
        return None

def _get_tenant_token(secret: str) -> Optional[str]:
    try:
        req = urllib.request.Request(
            'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            data=json.dumps({'app_id':os.environ.get('FEISHU_APP_ID','cli_aae86c7e07235bed'),'app_secret':secret}).encode(),
            headers={'Content-Type':'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10, context=_ctx(insecure=False)) as r:
            return json.loads(r.read()).get('tenant_access_token', '')
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f'[_get_tenant_token] 失败: {e}')
        return None

def _send_msg(token: str, msg: str) -> bool:
    payload = json.dumps({
        'receive_id': FEISHU_USER,
        'msg_type': 'text',
        'content': json.dumps({'text': msg})
    }).encode()
    req = urllib.request.Request(
        'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id',
        data=payload,
        headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10, context=_ctx(insecure=False)) as r:
            result = json.loads(r.read())
            if result.get('code') == 0:
                return True
            print(f'[_send_msg] API失败: {result}')
            return False
    except (urllib.error.URLError, json.JSONDecodeError) as e:
        print(f'[_send_msg] 异常: {e}')
        return False

def send_feishu(msg: str) -> bool:
    import os as _os
    if _os.environ.get("PUSH_DRYRUN"): return True
    secret = _get_feishu_secret()
    if not secret: return False
    token = _get_tenant_token(secret)
    if not token: return False
    return _send_msg(token, msg)

# ── 6. 主入口 ─────────────────────────────────────────────────
def main() -> bool:
    print('═══ 问天气象站 v8.0 飞书推送 (问天22维+LLM深度分析) ═══')

    # 1. 拉所有数据
    om = fetch_openmeteo() or {'current': {}, 'daily': {'time': []}, 'hourly': {}}
    uno = get_uno()

    # ⚠ 修复(2026-09-07): 推送前重新生成ultimate_forecast.json, 避免用29h前的陈腐数据
    import subprocess
    ult_ok = subprocess.run(
        [sys.executable, '/root/scripts/ultimate_predict.py', '--once'],
        capture_output=True, text=True, timeout=120
    )
    if ult_ok.returncode == 0:
        print('[Ultimate] ✅ 终极预测已重新生成')
    else:
        print(f'[Ultimate] ⚠ 重新生成失败 (rc={ult_ok.returncode}): {ult_ok.stderr[:200]}')
    ult = get_ult_fusion()
    chronos = get_chronos()
    kriging = get_kriging()
    alerts = get_alerts()
    # v1.1.2: 问天C程序22维度数据 (主人在 /root/scripts/wentian/)
    wentian = get_wentian()
    if wentian:
        print(f'[问天] 22维度数据已读取, ts={wentian.get("generated_at","?")}')
    else:
        print('[问天] ⚠ wentian_latest.json 不存在, 问天可能未运行')

    # v8.0: LLM深度分析
    print('[LLM] 调用DeepSeek分析多源数据...')
    import subprocess
    llm_ok = subprocess.run([sys.executable, '/root/scripts/llm_weather_analyst.py'],
                           capture_output=True, text=True, timeout=180)
    if llm_ok.returncode == 0:
        print('[LLM] ✅ 分析完毕')
    else:
        print(f'[LLM] ⚠ 分析失败 (rc={llm_ok.returncode}): {llm_ok.stderr[:200]}')

    # 2. 拼消息
    msg = build_message(om, uno, ult, chronos, kriging, alerts, wentian)

    # 3. 输出预览
    print('--- 推送内容 ---')
    print(msg)
    print('---')

    # 4. 发送
    ok = send_feishu(msg)
    print('✅ 飞书推送成功' if ok else '❌ 飞书推送失败')
    return ok

if __name__ == '__main__':
    main()
