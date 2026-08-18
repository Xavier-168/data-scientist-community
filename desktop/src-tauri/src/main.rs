// 发布版隐藏控制台窗口（debug 构建保留控制台便于调试）
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    data_scientist_lib::run();
}
