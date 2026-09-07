#!/usr/bin/env python3
"""
主人气象站 终极预测融合器 v3.0
输入:
  - Kriging预测 (回归+克里金残差)
  - Chronos-Bolt-Tiny零样本预测 (8M参数 T5模型)
  - Open-Meteo官方预测
  - UNO气压趋势
融合: 加权投票 + 置信区间合并
"""
import json, math, statistics
from datetime import datetime, timedelta
import os

FUSION_DIR = '/root/data/fusion'

def load_sources():
    sources = {}
    for name, path in [
        ('kriging', f'{FUSION_DIR}/kriging_result.json'),
        ('chronos', f'{FUSION_DIR}/chronos_result.json'),
        ('multi_source', f'{FUSION_DIR}/forecast.json'),
    ]:
        if os.path.exists(path):
            try:
                with open(path) as f:
                    sources[name] = json.load(f)
            except: pass
    return sources

def main_fuse():
    srcs = load_sources()
    print('═══ 终极预测融合 v3.0 ═══')
    print(f'可用数据源: {list(srcs.keys())}')

    # 默认权重(可调)
    WEIGHTS = {
        'kriging': 0.40,    # 克里金(主人算法,本地)
        'chronos': 0.35,     # Chronos零样本
        'multi_source': 0.25, # 多源投票
    }

    # ===== 温度预测融合 =====
    temp_1h = []
    temp_3h = []
    temp_6h = []
    print('\n═══ 温度预测融合 ═══')

    # 克里金: 只做气压,不做温度——不双倍计入Chronos
    if 'kriging' in srcs:
        kr = srcs['kriging']
        kr_p_mae = kr.get('pressure_mae', 0.5)
        if kr_p_mae is not None:
            print(f'  Kriging 气压训练回测 MAE: {kr_p_mae:.3f}hPa')

    if 'chronos' in srcs:
        ch = srcs['chronos']
        temp_1h.append((ch['temp']['1h_median'], WEIGHTS['chronos']))
        temp_3h.append((ch['temp']['3h_median'], WEIGHTS['chronos']))
        temp_6h.append((ch['temp']['6h_median'], WEIGHTS['chronos']))
        print(f'  Chronos 1h: {ch["temp"]["1h_median"]:.1f}°C')

    if 'multi_source' in srcs:
        ms = srcs['multi_source']
        # ⚠ 修复(2026-09-07): forecast.json的key是forecast_1h/3h/6h
        for key, lst in [('forecast_1h', temp_1h), ('forecast_3h', temp_3h), ('forecast_6h', temp_6h)]:
            fcst = ms.get(key, {})
            if isinstance(fcst, dict) and fcst.get('T'):
                lst.append((fcst['T'], WEIGHTS['multi_source']))
        # 打印
        cur = ms.get('current', {})
        if cur:
            print(f'  多源预测 当前: T={cur.get("T")}°C P={cur.get("P")}hPa wx={cur.get("wx")}')

    # 加权平均
    def weighted_avg(pairs):
        if not pairs: return None, 0
        total_w = sum(w for _, w in pairs)
        return sum(p*w for p, w in pairs) / total_w, total_w
    t1h, w1 = weighted_avg(temp_1h) if temp_1h else (None, 0)
    t3h, w3 = weighted_avg(temp_3h) if temp_3h else (None, 0)
    t6h, w6 = weighted_avg(temp_6h) if temp_6h else (None, 0)

    print(f'\n融合预测:')
    print(f'  温度1h: {t1h:.1f}°C' if t1h else '  温度1h: 暂无')
    print(f'  温度3h: {t3h:.1f}°C' if t3h else '  温度3h: 暂无')
    print(f'  温度6h: {t6h:.1f}°C' if t6h else '  温度6h: 暂无')

    # ===== 气压预测融合 =====
    pressure_1h = []
    pressure_3h = []
    print('\n═══ 气压预测融合 ═══')
    # ⚠ 注意: chronos使用MSL气压(~1013hPa), kriging/multi_source使用站压(~822hPa)
    # 站压→MSL转换复杂, 统一用Chronos(MSL)做气压融合, 不混用
    if 'chronos' in srcs:
        ch = srcs['chronos']
        pressure_1h.append((ch['pressure']['1h_median'], WEIGHTS['chronos'] + WEIGHTS['kriging']))
        pressure_3h.append((ch['pressure']['1h_median'], WEIGHTS['chronos'] + WEIGHTS['kriging'] + WEIGHTS['multi_source']))
        print(f'  Chronos 1h(MSL): {ch["pressure"]["1h_median"]:.1f}hPa (含kriging权重)')
        print(f'  ⚠ multi_source气压是站压(~822hPa), 跳过气压融合(不混用)')

    p1h, _ = weighted_avg(pressure_1h) if pressure_1h else (None, 0)
    p3h, _ = weighted_avg(pressure_3h) if pressure_3h else (None, 0)
    print(f'\n  气压1h: {p1h:.1f}hPa' if p1h else '  气压1h: 暂无')
    print(f'  气压3h: {p3h:.1f}hPa' if p3h else '  气压3h: 暂无')

    # ===== 置信区间(从Chronos) =====
    confidence = {}
    if 'chronos' in srcs:
        confidence['temp_1h_q10'] = srcs['chronos']['temp']['1h_q10']
        confidence['temp_1h_q90'] = srcs['chronos']['temp']['1h_q90']
        confidence['pressure_1h_q10'] = srcs['chronos']['pressure']['1h_q10']
        confidence['pressure_1h_q90'] = srcs['chronos']['pressure']['1h_q90']

    # ===== 天气投票 =====
    weather_votes = {}
    if 'multi_source' in srcs and 'weather_vote' in srcs['multi_source']:
        weather_votes = srcs['multi_source']['weather_vote']
    final_weather = max(weather_votes.items(), key=lambda x: x[1])[0] if weather_votes else '🌤 多云'

    # ===== 综合评分 =====
    score = 0
    alerts = []
    if 'multi_source' in srcs:
        ms = srcs['multi_source']
        score = ms.get('score', 0)
        alerts = ms.get('alerts', [])
        level = ms.get('level', 'NORMAL')
        color = ms.get('color', '#00e676')
    else:
        level, color = 'NORMAL', '#00e676'

    # ===== 推荐 =====
    recommendation = ''
    if p1h and p1h < 818:  # 气压低
        recommendation = '气压骤降,可能有恶劣天气'
    elif t1h and t1h > 35:
        recommendation = '高温预警,注意防暑'
    elif t1h and t1h < 10:
        recommendation = '低温预警,注意保暖'
    else:
        recommendation = '天气稳定,无特殊预警'

    final = {
        'ts': datetime.now().isoformat(timespec='seconds'),
        'method': 'ultimate_fusion_v3',
        'sources_used': list(srcs.keys()),
        'temperature': {
            '1h': t1h, '3h': t3h, '6h': t6h,
            'confidence': confidence
        },
        'pressure': {
            '1h': p1h, '3h': p3h,
            'confidence': confidence
        },
        'weather_final': final_weather,
        'weather_votes': weather_votes,
        'level': level,
        'color': color,
        'score': score,
        'alerts': alerts,
        'recommendation': recommendation,
        'individual': {
            'kriging_temp_mae': srcs.get('kriging', {}).get('temp_mae'),
            'kriging_pressure_mae': srcs.get('kriging', {}).get('pressure_mae'),
            'chronos_temp_mae': srcs.get('chronos', {}).get('backtest', {}).get('temp_mae'),
            'chronos_pressure_mae': srcs.get('chronos', {}).get('backtest', {}).get('pressure_mae'),
        }
    }
    with open(f'{FUSION_DIR}/ultimate_forecast.json', 'w') as f:
        json.dump(final, f, ensure_ascii=False, indent=2, default=str)
    print(f'\n✅ 终极预测存 {FUSION_DIR}/ultimate_forecast.json')
    return final

if __name__ == "__main__":
    import time, sys
    if "--once" in sys.argv:
        main_fuse()
    else:
        print("终极预测融合循环模式", flush=True)
        while True:
            try: main_fuse()
            except: pass
            time.sleep(300)
    main_fuse()