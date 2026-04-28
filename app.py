import os
import json
import time
import hashlib
import base64
import requests
import datetime
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import grpc

import clouddrive_pb2
import clouddrive_pb2_grpc

app = Flask(__name__)
CONFIG_FILE = "/app/data/config.json"
TEMP_DIR = "/app/data/temp_backups"
LOG_FILE = "/app/data/app.log"

# --- 📝 日志配置 ---
os.makedirs("/app/data", exist_ok=True)
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    handlers=[
                        RotatingFileHandler(LOG_FILE, maxBytes=2*1024*1024, backupCount=3, encoding='utf-8'),
                        logging.StreamHandler()
                    ])
logger = logging.getLogger(__name__)

default_config = {
    "ikuai_ip": "http://192.168.5.1",
    "ikuai_user": "admin",
    "ikuai_pass": "",
    "cd2_address": "192.168.5.2:19798",
    "cd2_user": "admin",
    "cd2_pass": "",
    "cd2_path": "/阿里云盘/爱快备份",
    "retain_days": 7,
    "cron_schedule": "0 3 * * *"
}

scheduler = BackgroundScheduler()

def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return default_config

def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)

class IKuaiManager:
    def __init__(self, ip, user, password):
        self.ip = ip
        self.user = user
        self.password = password
        self.session = requests.Session()
        self.headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "Origin": self.ip,
            "Referer": f"{self.ip}/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/147.0.0.0"
        }

    def login(self):
        url = f"{self.ip}/Action/login"
        md5_pass = hashlib.md5(self.password.encode('utf-8')).hexdigest()
        b64_pass = base64.b64encode(self.password.encode()).decode()
        try:
            res = self.session.post(url, json={"username": self.user, "passwd": md5_pass, "pass": b64_pass}, headers=self.headers, timeout=10).json()
            if res.get("Result") in [10000, 30000]:
                logger.info("✅ 爱快路由器：登录成功！")
                return True
            logger.error(f"❌ 爱快登录失败: {res}")
            return False
        except Exception as e:
            logger.error(f"❌ 爱快登录异常: {e}")
            return False

    def process_backup(self, save_dir):
        logger.info("⏳ 爱快路由器：正在下发创建备份指令...")
        self.session.post(f"{self.ip}/Action/call", json={"func_name": "backup", "action": "create", "param": {}}, headers=self.headers)
        time.sleep(2)
        
        res = self.session.post(f"{self.ip}/Action/call", json={"func_name": "backup", "action": "show", "param": {"TYPE": "data,disk"}}, headers=self.headers).json()
        backups = res.get("Data", {}).get("data", [])
        if not backups:
            logger.warning("⚠️ 未能获取到爱快备份文件列表。")
            return None
        filename = backups[0]['name']

        logger.info(f"⏳ 准备导出文件: {filename}")
        self.session.post(f"{self.ip}/Action/call", json={"func_name": "backup", "action": "EXPORT", "param": {"srcfile": filename}}, headers=self.headers)

        url_dl = f"{self.ip}/Action/download?filename={filename}"
        dl_headers = self.headers.copy()
        del dl_headers["Content-Type"]
        
        os.makedirs(save_dir, exist_ok=True)
        local_path = os.path.join(save_dir, filename)
        logger.info(f"⏳ 开始拉取备份文件到本地: {local_path}")
        with self.session.get(url_dl, headers=dl_headers, stream=True) as r:
            r.raise_for_status()
            with open(local_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        logger.info("✅ 本地下载完成。")
        return local_path

class CD2Manager:
    def __init__(self, address):
        self.channel = grpc.insecure_channel(address)
        self.stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(self.channel)
        self.jwt_token = None

    def login(self, user, password):
        req = clouddrive_pb2.GetTokenRequest(userName=user, password=password)
        try:
            res = self.stub.GetToken(req)
            if res.success:
                self.jwt_token = res.token
                logger.info("✅ CloudDrive2：登录成功！")
                return True
            logger.error(f"❌ CD2登录失败: {res.errorMessage}")
        except Exception as e:
            logger.error(f"❌ CD2登录异常: {e}")
        return False

    def upload_file(self, local_path, dest_dir):
        filename = os.path.basename(local_path)
        meta = [('authorization', f'Bearer {self.jwt_token}')]
        logger.info(f"⏳ CD2：开始上传 {filename} 到网盘目录...")
        
        c_req = clouddrive_pb2.CreateFileRequest(parentPath=dest_dir, fileName=filename)
        c_res = self.stub.CreateFile(c_req, metadata=meta)
        file_handle = c_res.fileHandle

        file_size = os.path.getsize(local_path)
        bytes_written = 0
        with open(local_path, 'rb') as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk: break
                is_last = (bytes_written + len(chunk)) >= file_size
                w_req = clouddrive_pb2.WriteFileRequest(fileHandle=file_handle, startPos=bytes_written, length=len(chunk), buffer=chunk, closeFile=is_last)
                self.stub.WriteToFile(w_req, metadata=meta)
                bytes_written += len(chunk)
        logger.info("✅ CD2：上传完成！")

    def clean_old_backups(self, dest_dir, retain_days):
        meta = [('authorization', f'Bearer {self.jwt_token}')]
        req = clouddrive_pb2.ListSubFileRequest(path=dest_dir, forceRefresh=True)
        files_to_delete = []
        now = datetime.datetime.utcnow()
        
        try:
            for res in self.stub.GetSubFiles(req, metadata=meta):
                for f in res.subFiles:
                    if not f.isDirectory and f.name.endswith(".bak"):
                        f_time = f.writeTime.ToDatetime()
                        if (now - f_time).days >= retain_days:
                            files_to_delete.append(f.fullPathName)
            
            if files_to_delete:
                logger.info(f"⏳ 发现 {len(files_to_delete)} 个过期备份，准备清理...")
                del_req = clouddrive_pb2.MultiFileRequest(path=files_to_delete)
                self.stub.DeleteFiles(del_req, metadata=meta)
                logger.info("✅ 清理过期备份完成。")
        except Exception as e:
            logger.error(f"❌ 清理旧备份失败: {e}")

def execute_backup_job():
    logger.info("==========================================")
    logger.info("🚀 触发备份任务")
    cfg = load_config()
    
    ikuai = IKuaiManager(cfg['ikuai_ip'], cfg['ikuai_user'], cfg['ikuai_pass'])
    cd2 = CD2Manager(cfg['cd2_address'])

    if ikuai.login():
        local_path = ikuai.process_backup(TEMP_DIR)
        if local_path and cd2.login(cfg['cd2_user'], cfg['cd2_pass']):
            cd2.upload_file(local_path, cfg['cd2_path'])
            cd2.clean_old_backups(cfg['cd2_path'], int(cfg['retain_days']))
            os.remove(local_path)
            logger.info("✅ 整个流水线已圆满结束，临时文件已清除。")
        else:
            logger.error("❌ CD2 登录失败或未获取到本地备份文件，流程终止。")
    else:
        logger.error("❌ 爱快路由器登录失败，流程终止。")

def update_scheduler():
    cfg = load_config()
    scheduler.remove_all_jobs()
    try:
        scheduler.add_job(execute_backup_job, CronTrigger.from_crontab(cfg['cron_schedule']))
        logger.info(f"⚙️ 调度器已更新，当前执行周期: {cfg['cron_schedule']}")
    except Exception as e:
        logger.error(f"❌ Cron 表达式解析失败: {e}")

# --- Web UI 路由 ---
@app.route('/')
def index():
    return render_template('index.html', config=load_config())

@app.route('/api/save', methods=['POST'])
def save_cfg():
    data = request.json
    save_config(data)
    update_scheduler()
    logger.info("💾 用户从 WebUI 保存了新配置并刷新了调度器。")
    return jsonify({"status": "success", "msg": "配置已保存并生效！"})

@app.route('/api/trigger', methods=['POST'])
def trigger_now():
    scheduler.add_job(execute_backup_job)
    logger.info("👉 用户从 WebUI 手动触发了立即执行。")
    return jsonify({"status": "success", "msg": "已在后台触发备份任务！请查看日志。"})

@app.route('/api/logs', methods=['GET'])
def get_logs():
    if not os.path.exists(LOG_FILE):
        return jsonify({"logs": "暂无日志..."})
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
        # 获取最后 100 行日志
        lines = f.readlines()[-100:]
        return jsonify({"logs": "".join(lines)})

@app.route('/api/clear_logs', methods=['POST'])
def clear_logs():
    open(LOG_FILE, 'w').close()
    return jsonify({"status": "success"})

if __name__ == '__main__':
    update_scheduler()
    scheduler.start()
    app.run(host='0.0.0.0', port=5000)