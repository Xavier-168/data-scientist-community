@echo off
chcp 65001 >nul
title 数据科学家 Community - 停止本地服务

rem 结束监听 8811 端口的本项目 runner 进程
rem /T 连同其 node/浏览器子进程整树终止，避免孤儿继续占用授权 profile
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r ":8811 .*LISTENING"') do (
    echo 正在结束进程树 PID %%p ...
    taskkill /F /T /PID %%p >nul 2>&1
)
echo 已停止。
pause
