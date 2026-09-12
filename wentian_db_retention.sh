#!/bin/bash
# 问天数据库保留策略 (2026-09-12): ano_weather.db已371MB(gps_log每秒1行, 81万行)
# 保留: gps_log 30天, ano_weather 180天, outdoor_weather 180天, ionosphere 90天
# 每月1日 04:30 执行 + incremental vacuum, eMMC 115GB 长期安全
set -e
DB=/root/data/ano_weather.db
sqlite3 "$DB" <<'SQL'
PRAGMA busy_timeout=30000;
DELETE FROM gps_log        WHERE ts < datetime('now','-30 day');
DELETE FROM ano_weather    WHERE ts < datetime('now','-180 day');
DELETE FROM outdoor_weather WHERE ts < datetime('now','-180 day');
DELETE FROM ionosphere     WHERE ts < datetime('now','-90 day');
PRAGMA incremental_vacuum;
SQL
# wentian.db 各表也裁一年 (它才11MB, 但防止长期漂移)
sqlite3 /root/data/wentian.db <<'SQL'
PRAGMA busy_timeout=30000;
DELETE FROM outdoor     WHERE ts < strftime('%s','now','-365 day');
DELETE FROM metar       WHERE ts < strftime('%s','now','-365 day');
DELETE FROM nowcast     WHERE ts < strftime('%s','now','-365 day');
DELETE FROM local_uno   WHERE ts < strftime('%s','now','-365 day');
DELETE FROM local_iono  WHERE ts < strftime('%s','now','-365 day');
DELETE FROM local_gnss  WHERE ts < strftime('%s','now','-365 day');
DELETE FROM swpc_kp     WHERE ts < strftime('%s','now','-365 day');
DELETE FROM external_data WHERE ts < strftime('%s','now','-365 day');
SQL
echo "$(date '+%F %T') retention done: $(ls -la $DB | awk '{print $5}') bytes"
