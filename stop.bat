@echo off
chcp 65001 >nul
title 数据科学家 Community - 停止本地服务

rem 结束监听 8811 端口的本项目 runner 进程
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r ":8811 .*LISTENING"') do (
    echo 正在结束进程 PID %%p ...
    taskkill /F /PID %%p >nul 2>&1
)
echo 已停止。
pause
