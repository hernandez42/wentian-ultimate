#!/usr/bin/env python3
"""
主人GNSS电离层闪烁指数计算器 v1.0
ATGM336H是单频,无L1/L2,无Klobuchar参数直接输出
但可用以下方法反推电离层活动:

1. S4 闪烁指数 = stddev(SNR) / mean(SNR)  (1分钟窗口)
   - S4 < 0.1: 电离层平静
   - S4 0.1-0.3: 弱扰动
   - S4 0.3-0.6: 中等闪烁 (信号失真)
   - S4 > 0.6: 强闪烁 (GPS精度下降)

2. ROTI (Rate of TEC Index) = |dTEC/dt|/1min
   - 单频下从SNR斜率反推

3. 卫星SNR异常检测
   - 北斗22 SNR持续>40 = 闪烁活跃

4. SBAS可用性间接反映电离层
"""
import sqlite3, statistics, math, time
from datetime import datetime, timedelta

DB = '/root/data/ano_weather.db'

def calc_s4_snr_window(snr_values):
    """S4 = stddev/mean"""
    if len(snr_values) < 5: return None
    mean = statistics.mean(snr_values)
    if mean <= 0: return None
    sd = statistics.stdev(snr_values) if len(snr_values) > 1 else 0
    return sd / mean

def calc_s4_for_window(c, lookback_min=5):
    """最近N分钟的GPS/BDS SNR计算S4"""
    since = (datetime.now() - timedelta(minutes=lookback_min)).isoformat()
    rows = c.execute("""SELECT ts, gps_snr_avg, bds_snr_avg FROM gps_log
                        WHERE ts > ? AND (gps_snr_avg IS NOT NULL OR bds_snr_avg IS NOT NULL)
                        ORDER BY ts""", (since,)).fetchall()
    if not rows: return None
    gps_snrs = [r[1] for r in rows if r[1] is not None and r[1] > 0]
    bds_snrs = [r[2] for r in rows if r[2] is not None and r[2] > 0]
    return {
        's4_gps': calc_s4_snr_window(gps_snrs),
        's4_bds': calc_s4_snr_window(bds_snrs),
        'samples_gps': len(gps_snrs),
        'samples_bds': len(bds_snrs),
        'mean_gps_snr': statistics.mean(gps_snrs) if gps_snrs else None,
        'mean_bds_snr': statistics.mean(bds_snrs) if bds_snrs else None,
    }

def klobuchar_model(lat, lon, alt_m, utc_time, alpha_params=None, beta_params=None):
    """Klobuchar电离层延迟模型 - 单频GPS用户标准模型
    返回L1频段垂直延迟(秒)和斜向延迟(秒)
    默认alpha/beta参数来自广播星历 (如果没传入则用典型参数)
    """
    # 默认alpha参数(典型值 - 实际应从导航电文获取)
    if alpha_params is None:
        alpha_params = [1.1e-8, -7.6e-9, -5.6e-7, 5.7e-8]
    if beta_params is None:
        beta_params = [91136, 65536, -393216, 393216]

    # 地心角(秒)
    psi = 0.0137 / (alt_m / 1000.0 + 0.11) - 0.022

    # 测站地心纬度(半圆)
    phi = lat / 180.0  # 大圆地心纬度
    phi_i = phi + psi * math.cos(math.pi * lat / 180.0)
    if phi_i > 0.416: phi_i = 0.416
    if phi_i < -0.416: phi_i = -0.416

    # 测站地心经度(半圆)
    lam = lon / 180.0

    # 地方时(秒)
    t = 4.32e4 * lam + utc_time
    t = t % 86400
    if t < 0: t += 86400

    # 倾斜因子
    f = 1.0 + 16.0 * (0.53 - alt_m / 1000.0 / 57.3) ** 3

    # 周期(秒)
    p = beta_params[0] + beta_params[1] * (phi_i / math.pi) + beta_params[2] * (phi_i / math.pi)**2 + beta_params[3] * (phi_i / math.pi)**3
    if p < 72000: p = 72000

    # 相位(秒)
    x = 2 * math.pi * (t - 50400) / p

    # 振幅(秒)
    if abs(x) > math.pi / 2:
        amp = f * 5e-9
    else:
        amp = f * (alpha_params[0] + alpha_params[1] * (phi_i/math.pi) +
                   alpha_params[2] * (phi_i/math.pi)**2 + alpha_params[3] * (phi_i/math.pi)**3)
        if amp < 0: amp = 0

    # 垂直延迟(秒)
    if abs(x) > math.pi / 2:
        T_vert = f * 5e-9
    else:
        T_vert = amp * (1 - x*x/2 + x*x*x*x/24)

    return {
        'vertical_delay_s': T_vert,
        'slant_delay_s': f * T_vert,
        'slant_factor': f,
        'period_s': p,
        'amplitude_s': amp,
        'earth_rot_angle': psi,
        'geomagnetic_lat': phi_i
    }

def get_ionosphere_data():
    """主函数: 计算电离层相关数据"""
    c = sqlite3.connect(DB)
    s4_data = calc_s4_for_window(c, lookback_min=5)
    # 最近GPS SNR数据
    recent = c.execute("""SELECT ts, gps_sats, bds_sats, gps_snr_avg, bds_snr_avg,
                                pdop, vdop, hdop, speed_kts, heading_deg, alt, fix
                          FROM gps_log WHERE ts > datetime('now','-10 minute')
                          ORDER BY ts DESC LIMIT 30""").fetchall()
    c.close()

    if not recent:
        return {'error': 'no recent GPS data'}

    # 平均SNR(过去10min)
    gps_snrs = [r[3] for r in recent if r[3] is not None]
    bds_snrs = [r[4] for r in recent if r[4] is not None]
    pdops = [r[5] for r in recent if r[5] is not None and r[5] < 10]
    vdops = [r[6] for r in recent if r[6] is not None and r[6] < 10]

    avg_gps_snr = statistics.mean(gps_snrs) if gps_snrs else None
    avg_bds_snr = statistics.mean(bds_snrs) if bds_snrs else None
    avg_pdop = statistics.mean(pdops) if pdops else None
    avg_vdop = statistics.mean(vdops) if vdops else None

    # Klobuchar模型预测(昆明长水: 25.09917°N 102.92667°E, 海拔2115m)
    now = datetime.utcnow()
    utc_sec = now.hour * 3600 + now.minute * 60 + now.second
    klob = klobuchar_model(25.09917, 102.92667, 2103, utc_sec)

    # 电离层活动评估
    # ⚠ 修复(2026-09-12): 旧键名's4gps'拼错(实际是's4_gps') → GPS S4
    # 在activity判定中恒被忽略, 只看北斗造成STRONG误报。
    activity = 'UNKNOWN'
    if s4_data:
        _g = s4_data.get('s4_gps') or 0
        _b = s4_data.get('s4_bds') or 0
        s4_max = max(_g, _b)
        if s4_max < 0.05: activity = 'QUIET'
        elif s4_max < 0.15: activity = 'WEAK'
        elif s4_max < 0.30: activity = 'MODERATE'
        else: activity = 'STRONG'

    return {
        'ts': datetime.now().isoformat(timespec='seconds'),
        's4': s4_data,
        'snr_avg': {
            'gps_snr': round(avg_gps_snr, 1) if avg_gps_snr else None,
            'bds_snr': round(avg_bds_snr, 1) if avg_bds_snr else None,
            'pdop_avg': round(avg_pdop, 2) if avg_pdop else None,
            'vdop_avg': round(avg_vdop, 2) if avg_vdop else None
        },
        'klobuchar': klob,
        'ionosphere_activity': activity,
        'local_time_utc': utc_sec
    }

def save_to_db(data):
    if 'error' in data: return
    c = sqlite3.connect(DB)
    c.execute('''CREATE TABLE IF NOT EXISTS ionosphere (
        ts TEXT PRIMARY KEY,
        s4_gps REAL, s4_bds REAL,
        samples_gps INTEGER, samples_bds INTEGER,
        gps_snr_avg REAL, bds_snr_avg REAL,
        pdop_avg REAL, vdop_avg REAL,
        klob_vert_delay REAL, klob_slant_delay REAL,
        klob_slant_factor REAL, klob_period_s REAL,
        klob_amplitude REAL, klob_geomag_lat REAL,
        activity TEXT
    )''')
    s4 = data.get('s4') or {}
    snr = data.get('snr_avg') or {}
    k = data.get('klobuchar') or {}
    c.execute('''INSERT OR REPLACE INTO ionosphere VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (data['ts'],
         s4.get('s4_gps'), s4.get('s4_bds'),
         s4.get('samples_gps'), s4.get('samples_bds'),
         snr.get('gps_snr'), snr.get('bds_snr'),
         snr.get('pdop_avg'), snr.get('vdop_avg'),
         k.get('vertical_delay_s'), k.get('slant_delay_s'),
         k.get('slant_factor'), k.get('period_s'),
         k.get('amplitude_s'), k.get('geomagnetic_lat'),
         data.get('ionosphere_activity')))
    c.commit()
    c.close()

def main_loop():
    """循环模式:每分钟算一次"""
    print('═══ GNSS电离层闪烁监测 v2.0 (循环模式) ═══', flush=True)
    print(f'位置: 25.09917°N 102.92667°E 海拔2103m', flush=True)
    while True:
            data = get_ionosphere_data()
            save_to_db(data)
            # 打印状态
            # ⚠ 修复(2026-09-12): dict.get(k,0)在键存在但值为None时仍返回None,
            # f'{None:.3f}'抛TypeError → 服务重启死循环。统一nf()兜底。
            def nf(v, d=1):
                return f'{v:.{d}f}' if isinstance(v, (int, float)) else 'N/A'
            if 's4' in data and data['s4']:
                s4 = data['s4']
                print(f'[{data["ts"]}] {data.get("ionosphere_activity", "?")} | '
                      f'GPS S4={nf(s4.get("s4_gps"), 3)} BDS S4={nf(s4.get("s4_bds"), 3)} | '
                      f'SNR: GPS={nf(data["snr_avg"].get("gps_snr"))}dB BDS={nf(data["snr_avg"].get("bds_snr"))}dB', flush=True)
            time.sleep(60)  # 1分钟一次

if __name__ == '__main__':
    import sys
    if '--once' in sys.argv or '--json' in sys.argv:
        data = get_ionosphere_data()
        save_to_db(data)
        if '--json' in sys.argv:
            import json
            print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        else:
            # 简要输出 — ⚠ 修复(2026-09-12): S4/mean可为None(样本不足),
            # 旧版 f'{None:.3f}' 抛TypeError → Type=simple服务重启9次死循环
            print(f'[{data["ts"]}]')
            if 's4' in data and data['s4']:
                _g = data['s4'].get('s4_gps')
                _b = data['s4'].get('s4_bds')
                print(f'  S4: GPS={_g:.3f}' if _g is not None else '  S4: GPS=无样本',
                      end='')
                print(f' BDS={_b:.3f}' if _b is not None else ' BDS=无样本')
            print(f'  SNR avg: GPS={data["snr_avg"]["gps_snr"]} BDS={data["snr_avg"]["bds_snr"]}')
            print(f'  Klobuchar vert_delay={data["klobuchar"]["vertical_delay_s"]*1e9:.2f}ns')
            print(f'  Activity: {data["ionosphere_activity"]}')
    else:
        main_loop()