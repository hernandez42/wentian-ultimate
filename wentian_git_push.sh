#!/bin/bash
# ════════════════════════════════════════════════════════════════
# 问天 GitHub 自动推送脚本 - v1.0
# ════════════════════════════════════════════════════════════════
# 功能: 检测 wentian 源码变化, 自动 commit + push 到 GitHub
# 触发: cron 每小时检查 (可在 cron 表里调整)
# 主人: 朱涛 BG8SBA, 昆明长水机场
# 仓库: git@github.com:hernandez42/wentian-v2.1.git
# 分支: master
# ════════════════════════════════════════════════════════════════

set -e

REPO_DIR="/root/scripts/wentian"
LOG="/var/log/wentian_git_push.log"
LOCK="/tmp/wentian_git_push.lock"

# 防止并发
if [ -f "$LOCK" ]; then
    LOCK_AGE=$(($(date +%s) - $(stat -c %Y "$LOCK" 2>/dev/null || echo 0)))
    if [ $LOCK_AGE -lt 600 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') [skip] 锁定中(${LOCK_AGE}s), 跳过本次" >> "$LOG"
        exit 0
    else
        rm -f "$LOCK"
    fi
fi
touch "$LOCK"
trap 'rm -f "$LOCK"' EXIT

cd "$REPO_DIR" || { echo "$(date '+%Y-%m-%d %H:%M:%S') [err] 无法进入 $REPO_DIR" >> "$LOG"; exit 1; }

# 检查 git 是否就绪
if [ ! -d ".git" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [err] 非 git 仓库" >> "$LOG"
    exit 1
fi

# 检查远程连通性(超时 10s)
if ! timeout 10 git ls-remote origin master >/dev/null 2>&1; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [warn] 远程不可达, 跳过 push" >> "$LOG"
    exit 0
fi

# 检查是否有变更
CHANGED=$(git status --porcelain 2>/dev/null | wc -l)
if [ "$CHANGED" -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ok] 无变更, 不推送" >> "$LOG"
    exit 0
fi

# 自动生成 commit 信息
TIMESTAMP=$(date '+%Y-%m-%d %H:%M')
SOURCES_CHANGED=$(git status --porcelain | awk '{print $2}' | grep -E '\.(c|h|md)$' | wc -l)
NEW_FILES=$(git status --porcelain | grep '^??' | awk '{print $2}' | wc -l)
MODIFIED=$(git status --porcelain | grep '^ M' | awk '{print $2}' | wc -l)

# 取 daemon 最近评分作为 commit msg 的一部分
RECENT_SCORE=$(sqlite3 /root/data/wentian.db \
    "SELECT total_score FROM evolution ORDER BY ts DESC LIMIT 1;" 2>/dev/null || echo "N/A")

COMMIT_MSG="问天自动同步 ${TIMESTAMP} | 评分=${RECENT_SCORE} | +${NEW_FILES}新文件 ~${MODIFIED}修改"

echo "$(date '+%Y-%m-%d %H:%M:%S') [info] 发现 ${CHANGED} 项变更, 准备 commit" >> "$LOG"

# git add
git add -A 2>>"$LOG" || { echo "$(date '+%Y-%m-%d %H:%M:%S') [err] git add 失败" >> "$LOG"; exit 1; }

# git commit (用户配置)
git -c user.name="hernandez42" \
    -c user.email="hernandez42@users.noreply.github.com" \
    commit -m "$COMMIT_MSG" >>"$LOG" 2>&1 || {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [err] git commit 失败" >> "$LOG"
    exit 1
}

# git push
# ⚠ 修复(2026-09-07): 原HTTPS token已过期, 改用SSH key
if timeout 10 git push origin master >>"$LOG" 2>&1; then
    COMMIT_HASH=$(git rev-parse --short HEAD)
    echo "$(date '+%Y-%m-%d %H:%M:%S') [ok] 推送成功 ${COMMIT_HASH}: ${COMMIT_MSG}" >> "$LOG"

    # 飞书推送通知 (可选, 不阻塞)
    if [ -f /root/scripts/wentian/push_alert.py ]; then
        MSG="📦 问天 v2.3 GitHub推送成功
仓库: hernandez42/wentian-v2.1
提交: ${COMMIT_HASH}
内容: ${COMMIT_MSG}
评分: ${RECENT_SCORE}"
        # 写入临时文件供 push_alert.py 读取
        echo "{\"ts\": $(date +%s), \"title\":\"GitHub同步\", \"text\":\"${MSG}\"}" > /tmp/wentian_git_push_notice.json
    fi
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') [err] git push 失败, 见上面日志" >> "$LOG"
    exit 1
fi

exit 0
