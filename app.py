import os
import json
import time
import hashlib
import base64
import requests
import datetime
import logging
from logging.handlers import RotatingFileHandler
from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import grpc

import clouddrive_pb2
import clouddrive_pb2_grpc

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())

CONFIG_FILE = "/app/data/config.json"
TEMP_DIR = "/app/data/temp_backups"
LOG_FILE = "/app/data/app.log"

# --- 🔐 网页安全认证 ---
WEB_USER = os.getenv("WEB_USER", "admin")
WEB_PASS = os.getenv("WEB_PASS", "admin123")

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.json
        if data.get('username') == WEB_USER and data.get('password') == WEB_PASS:
            session['logged_in'] = True
            return jsonify({"status": "success", "msg": "登录成功"})
        return jsonify({"status": "error", "msg": "账号或密码错误"}), 401
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.before_request
def require_auth():
    if request.endpoint not in ['login', 'static'] and not session.get('logged_in'):
        if request.path.startswith('/api/'):
            return jsonify({"status": "error", "msg": "未登录"}), 401
        return redirect(url_for('login'))

# --- 📝 日志配置优化 ---
os.makedirs("/app/data", exist_ok=True)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

logger = logging.getLogger("ikuai_backup")
logger.setLevel(logging.INFO)
formatter = logging.Formatter('[%(asctime)s] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2*1024*1024, backupCount=3, encoding='utf-8')
file_handler.setFormatter(formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)

if not logger.handlers:
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

# --- 默认配置 ---
default_config = {
    "ikuai_ip": "http://192.168.5.1",
    "ikuai_user": "admin",
    "ikuai_pass": "",
    "cd2_address": "192.168.5.2:19798",
    "cd2_user": "admin",
    "cd2_pass": "",
    "cd2_path": "/阿里云盘/爱快备份",
    "retain_days": 7,
    "local_retain_days": 3,  # 新增：路由本地默认保留3天
    "cron_schedule": "0 3 * * *"
}

scheduler = BackgroundScheduler()

def load_config():
    # 智能合并配置：防止旧版 config.json 缺少新字段报错
    cfg = default_config.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                user_cfg = json.load(f)
                cfg.update(user_cfg)
        except Exception:
            pass
    return cfg

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
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
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
        logger.info("⏳ 正在向爱快下发创建备份指令...")
        self.session.post(f"{self.ip}/Action/call", json={"func_name": "backup", "action": "create", "param": {}}, headers=self.headers)
        time.sleep(2)
        
        res = self.session.post(f"{self.ip}/Action/call", json={"func_name": "backup", "action": "show", "param": {"TYPE": "data,disk"}}, headers=self.headers).json()
        backups = res.get("Data", {}).get("data", [])
        if not backups:
            logger.warning("⚠️ 未能获取到爱快备份文件列表。")
            return None, None
        filename = backups[0]['name']

        logger.info(f"⏳ 准备导出文件: {filename}")
        self.session.post(f"{self.ip}/Action/call", json={"func_name": "backup", "action": "EXPORT", "param": {"srcfile": filename}}, headers=self.headers)

        url_dl = f"{self.ip}/Action/download?filename={filename}"
        dl_headers = self.headers.copy()
        if "Content-Type" in dl_headers:
            del dl_headers["Content-Type"]
        
        os.makedirs(save_dir, exist_ok=True)
        local_path = os.path.join(save_dir, filename)
        logger.info(f"⏳ 开始拉取备份文件到系统缓存...")
        with self.session.get(url_dl, headers=dl_headers, stream=True) as r:
            r.raise_for_status()
            with open(local_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        logger.info(f"✅ 缓存下载完成: {filename}")
        return local_path, filename

    def delete_backup(self, filename):
        url = f"{self.ip}/Action/call"
        payload = {"func_name": "backup", "action": "delete", "param": {"srcfile": filename}}
        try:
            self.session.post(url, json=payload, headers=self.headers, timeout=10)
        except Exception as e:
            logger.error(f"❌ 爱快删除异常: {e}")

    def clean_old_backups(self, retain_days):
        logger.info(f"⏳ 正在检查爱快路由器本地历史备份 (策略: 保留 {retain_days} 天)...")
        res = self.session.post(f"{self.ip}/Action/call", json={"func_name": "backup", "action": "show", "param": {"TYPE": "data,disk"}}, headers=self.headers).json()
        backups = res.get("Data", {}).get("data", [])
        
        now = datetime.datetime.now()
        deleted_count = 0
        for b in backups:
            name = b.get("name")
            date_str = b.get("date")  # 例如: "2026-04-28 20:14:17"
            if name and date_str:
                try:
                    b_date = datetime.datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                    if (now - b_date).days >= retain_days:
                        self.delete_backup(name)
                        deleted_count += 1
                except Exception as e:
                    logger.warning(f"⚠️ 解析备份日期失败: {date_str}, {e}")
        
        if deleted_count > 0:
            logger.info(f"✅ 爱快路由清理完成：共释放了 {deleted_count} 个过期本地备份包。")
        else:
            logger.info("✅ 爱快路由状态健康：暂无过期本地备份需清理。")

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
        logger.info(f"⏳ CD2：开始推送 {filename} 到云端...")
        
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
        logger.info("✅ CD2：云端上传完成！")

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
                logger.info(f"⏳ CD2云盘：发现 {len(files_to_delete)} 个过期备份，正在执行云端清理...")
                del_req = clouddrive_pb2.MultiFileRequest(path=files_to_delete)
                self.stub.DeleteFiles(del_req, metadata=meta)
                logger.info("✅ CD2云盘清理完成。")
            else:
                logger.info("✅ CD2云盘状态健康：暂无过期备份。")
        except Exception as e:
            logger.error(f"❌ 云端清理旧备份失败: {e}")

def execute_backup_job():
    logger.info("==========================================")
    logger.info("🚀 启动全链路自动化备份任务")
    cfg = load_config()
    
    ikuai = IKuaiManager(cfg['ikuai_ip'], cfg['ikuai_user'], cfg['ikuai_pass'])
    cd2 = CD2Manager(cfg['cd2_address'])

    if ikuai.login():
        local_path, filename = ikuai.process_backup(TEMP_DIR)
        if local_path and cd2.login(cfg['cd2_user'], cfg['cd2_pass']):
            
            # 1. 核心流程：同步至云端
            cd2.upload_file(local_path, cfg['cd2_path'])
            
            # 2. 生命周期管理：清理云端旧备份
            cd2.clean_old_backups(cfg['cd2_path'], int(cfg['retain_days']))
            
            # 3. 生命周期管理：清理爱快路由器本地旧备份
            ikuai.clean_old_backups(int(cfg['local_retain_days']))
            
            # 4. 环境清理：抹除 Docker 容器内下载缓存
            os.remove(local_path)
            
            logger.info("🎉 备份流水线圆满结束！云/端双备份清理策略已执行。")
        else:
            logger.error("❌ CD2登录失败或备份拉取失败，流程终止。")
    else:
        logger.error("❌ 爱快路由器登录失败，流程终止。")
    logger.info("==========================================")

def update_scheduler():
    cfg = load_config()
    scheduler.remove_all_jobs()
    try:
        scheduler.add_job(execute_backup_job, CronTrigger.from_crontab(cfg['cron_schedule']))
        logger.info(f"⚙️ 调度器已重载 | Cron: {cfg['cron_schedule']} | 云保留: {cfg['retain_days']}天 | 路由保留: {cfg['local_retain_days']}天")
    except Exception as e:
        logger.error(f"❌ Cron 表达式错误: {e}")

# --- 路由 ---
@app.route('/')
def index():
    return render_template('index.html', config=load_config())

@app.route('/api/save', methods=['POST'])
def save_cfg():
    data = request.json
    save_config(data)
    update_scheduler()
    logger.info("💾 节点配置已从网页端更新。")
    return jsonify({"status": "success", "msg": "配置已保存并生效！"})

@app.route('/api/trigger', methods=['POST'])
def trigger_now():
    scheduler.add_job(execute_backup_job)
    return jsonify({"status": "success", "msg": "已触发，请查看日志！"})

@app.route('/api/logs', methods=['GET'])
def get_logs():
    if not os.path.exists(LOG_FILE):
        return jsonify({"logs": "暂无日志..."})
    with open(LOG_FILE, 'r', encoding='utf-8') as f:
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
