"""
Flask服务自动重启守护进程
检测主进程是否存活，崩溃后自动重启
"""
import os
import sys
import time
import subprocess
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 配置
CHECK_INTERVAL = 10  # 每10秒检查一次
MAX_RESTARTS = 5     # 最多连续重启次数
RESTART_COOLDOWN = 30  # 重启冷却时间（秒）


def get_main_pid():
    """获取当前进程的PID"""
    return os.getpid()


def is_process_alive(pid):
    """检查进程是否存活"""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Windows上PermissionError表示进程存在
        return True


def restart_flask():
    """重启Flask服务"""
    app_dir = Path(__file__).parent
    python_exe = sys.executable
    
    logger.info("正在重启Flask服务...")
    
    try:
        # 使用当前Python解释器启动serve.py
        proc = subprocess.Popen(
            [python_exe, "serve.py"],
            cwd=str(app_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        logger.info(f"Flask服务已重启，新PID: {proc.pid}")
        return proc.pid
    except Exception as e:
        logger.error(f"重启失败: {e}")
        return None


def run_monitor():
    """运行监控守护进程"""
    main_pid = get_main_pid()
    restart_count = 0
    last_restart_time = 0
    
    logger.info(f"Flask监控守护进程启动，主进程PID: {main_pid}")
    
    while True:
        time.sleep(CHECK_INTERVAL)
        
        # 检查主进程是否存活
        if not is_process_alive(main_pid):
            logger.warning(f"主进程 {main_pid} 已停止，尝试重启...")
            
            # 检查是否在冷却期内
            current_time = time.time()
            if current_time - last_restart_time < RESTART_COOLDOWN:
                logger.warning("仍在冷却期，跳过重启")
                continue
            
            # 检查连续重启次数
            if restart_count >= MAX_RESTARTS:
                logger.error(f"连续重启 {MAX_RESTARTS} 次，停止自动重启")
                break
            
            # 尝试重启
            new_pid = restart_flask()
            if new_pid:
                main_pid = new_pid
                restart_count += 1
                last_restart_time = current_time
                logger.info(f"重启成功，新的主进程PID: {main_pid}")
            else:
                logger.error("重启失败，将等待后重试")
                time.sleep(RESTART_COOLDOWN)
        else:
            # 进程存活，重置计数
            if restart_count > 0:
                logger.info("服务已恢复正常")
                restart_count = 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run_monitor()
