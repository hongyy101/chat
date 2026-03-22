# main.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from typing import List, Dict, Optional
import uvicorn
import socket
import json
import hashlib
from datetime import datetime
from pathlib import Path

app = FastAPI(title="简·简")
templates = Jinja2Templates(directory="templates")

# 配置文件
CONFIG_FILE = "device_config.json"

class DeviceManager:
    """设备管理类，用于绑定设备"""
    def __init__(self):
        self.devices: Dict[str, str] = {}  # device_id -> nickname
        self.load_config()
    
    def load_config(self):
        """加载设备配置"""
        try:
            if Path(CONFIG_FILE).exists():
                with open(CONFIG_FILE, 'r') as f:
                    self.devices = json.load(f)
                print(f"📱 加载设备配置: {len(self.devices)} 个设备")
        except Exception as e:
            print(f"加载配置失败: {e}")
    
    def save_config(self):
        """保存设备配置"""
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.devices, f, indent=2)
            print(f"💾 保存设备配置: {len(self.devices)} 个设备")
        except Exception as e:
            print(f"保存配置失败: {e}")
    
    def register_device(self, device_id: str, nickname: str) -> bool:
        """注册设备"""
        if device_id not in self.devices:
            self.devices[device_id] = nickname
            self.save_config()
            return True
        return False
    
    def is_authorized(self, device_id: str) -> bool:
        """检查设备是否已授权"""
        return device_id in self.devices
    
    def get_device_nickname(self, device_id: str) -> str:
        """获取设备昵称"""
        return self.devices.get(device_id, "未知设备")
    
    def get_device_count(self) -> int:
        """获取设备数量"""
        return len(self.devices)
    
    def get_device_id_list(self) -> List[str]:
        """获取所有设备ID列表"""
        return list(self.devices.keys())

class MessageStore:
    """消息存储类，用于保存离线消息"""
    def __init__(self, max_messages=100):
        self.messages: List[Dict] = []  # 所有离线消息
        self.max_messages = max_messages
    
    def add_message(self, from_device: str, from_nickname: str, content: str):
        """添加离线消息"""
        message = {
            "from_device": from_device,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "time_str": datetime.now().strftime("%H:%M")
        }
        self.messages.append(message)
        
        # 限制最大消息数
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]
        
        print(f"📦 保存离线消息: {content[:20]}...")
    
    def get_messages(self) -> List[Dict]:
        """获取所有离线消息"""
        messages = self.messages.copy()
        self.messages.clear()  # 清空离线消息
        return messages
    
    def has_messages(self) -> bool:
        """是否有离线消息"""
        return len(self.messages) > 0

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.connection_device: Dict[WebSocket, str] = {}  # websocket -> device_id
        self.device_connection: Dict[str, WebSocket] = {}  # device_id -> websocket
        self.device_manager = DeviceManager()
        self.message_store = MessageStore()

    async def connect(self, websocket: WebSocket, device_id: str, nickname: str) -> bool:
        """用户连接处理（注意：websocket已经accept过了）"""
        # 检查设备是否已授权
        if not self.device_manager.is_authorized(device_id):
            # 如果是第一个设备，自动授权
            if self.device_manager.get_device_count() == 0:
                self.device_manager.register_device(device_id, nickname)
                await websocket.send_json({
                    "type": "system",
                    "content": f"✨ 设备已注册，欢迎！"
                })
            else:
                # 已有设备，返回设备ID列表供管理员添加
                device_list = self.device_manager.get_device_id_list()
                await websocket.send_json({
                    "type": "unauthorized",
                    "device_id": device_id,
                    "nickname": nickname,
                    "registered_devices": device_list,
                    "message": "未授权设备，请联系设备管理员。"
                })
                await websocket.close(code=1008, reason="未授权设备")
                return False
        
        # 检查是否已有设备在线（只允许两个设备同时在线）
        if len(self.active_connections) >= 2:
            await websocket.send_json({
                "type": "error",
                "content": "❌ 聊天室已满，只有两个设备可以同时聊天。"
            })
            await websocket.close(code=1008, reason="聊天室已满")
            return False
        
        # 如果同一设备重复连接，先断开旧连接
        if device_id in self.device_connection:
            old_ws = self.device_connection[device_id]
            await self.disconnect(old_ws)
        
        # 添加新连接
        self.active_connections.append(websocket)
        self.connection_device[websocket] = device_id
        self.device_connection[device_id] = websocket
        
        print(f"✅ 设备 [{nickname}] 加入，当前连接数: {len(self.active_connections)}")
        
        # 发送欢迎消息
        await websocket.send_json({
            "type": "system",
            "content": f"✅ 已连接到服务器"
        })
        
        # 检查是否有离线消息
        if self.message_store.has_messages():
            offline_messages = self.message_store.get_messages()
            await websocket.send_json({
                "type": "offline_messages",
                "messages": offline_messages
            })
        
        # 通知对方设备有设备上线
        await self.notify_other_device(websocket, f"✨ 对方上线了")
        
        return True

    async def disconnect(self, websocket: WebSocket):
        """用户断开连接"""
        if websocket in self.active_connections:
            device_id = self.connection_device.get(websocket)
            nickname = self.device_manager.get_device_nickname(device_id) if device_id else "未知"
            
            self.active_connections.remove(websocket)
            if websocket in self.connection_device:
                del self.connection_device[websocket]
            if device_id and device_id in self.device_connection:
                del self.device_connection[device_id]
            
            print(f"👋 设备 [{nickname}] 离开，当前连接数: {len(self.active_connections)}")
            
            # 通知对方设备
            await self.notify_other_device(websocket, f"👋 对方下线了")

    async def notify_other_device(self, sender: WebSocket, message: str):
        """通知另一个设备"""
        for connection in self.active_connections:
            if connection != sender:
                try:
                    await connection.send_json({
                        "type": "system",
                        "content": message
                    })
                except Exception as e:
                    print(f"发送通知失败: {e}")

    async def send_message(self, message: str, sender: WebSocket):
        """发送消息给另一个设备"""
        sender_device_id = self.connection_device.get(sender)
        
        # 查找接收者
        receiver = None
        for connection in self.active_connections:
            if connection != sender:
                receiver = connection
                break
        
        if receiver:
            # 对方在线，直接发送
            try:
                await receiver.send_json({
                    "type": "message",
                    "content": message,
                    "timestamp": datetime.now().isoformat(),
                    "time_str": datetime.now().strftime("%H:%M")
                })
                
                # 给发送者回执（静默，不显示）
                print(f"📤 发送消息成功")
            except Exception as e:
                print(f"发送失败: {e}")
                # 发送失败，保存为离线消息
                self.message_store.add_message(sender_device_id, "", message)
        else:
            # 对方离线，保存消息
            self.message_store.add_message(sender_device_id, "", message)
            print(f"📦 对方离线，保存消息")

manager = ConnectionManager()

@app.get("/", response_class=HTMLResponse)
async def get(request: Request):
    return templates.TemplateResponse("chat.html", {"request": request})

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket端点 - 修复版本"""
    # 关键修复：立即接受连接，完成WebSocket握手
    await websocket.accept()
    
    try:
        # 接收设备信息
        device_info = await websocket.receive_text()
        device_data = json.loads(device_info)
        device_id = device_data.get("device_id")
        nickname = device_data.get("nickname", "用户")
        
        if not device_id:
            # 已经accept了，现在关闭连接
            await websocket.close(code=1008, reason="缺少设备ID")
            return
        
        # 连接处理
        success = await manager.connect(websocket, device_id, nickname)
        if not success:
            # connect方法中已经处理了close，直接返回
            return
        
        # 消息循环
        while True:
            data = await websocket.receive_text()
            try:
                message_data = json.loads(data)
                if message_data.get("type") == "message":
                    content = message_data.get("content", "")
                    if content:
                        await manager.send_message(content, websocket)
            except json.JSONDecodeError:
                # 纯文本消息
                if data.strip():
                    await manager.send_message(data, websocket)
                
    except WebSocketDisconnect:
        # 客户端主动断开
        await manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket错误: {e}")
        # 尝试正常断开连接
        try:
            if websocket.client_state.value == 1:  # 1表示CONNECTED
                await manager.disconnect(websocket)
        except:
            pass

@app.get("/devices")
async def get_devices():
    """获取设备列表"""
    return {
        "count": manager.device_manager.get_device_count(),
        "devices": manager.device_manager.get_device_id_list()
    }

@app.get("/config")
async def get_config():
    """获取配置信息"""
    return {
        "registered_devices": manager.device_manager.get_device_id_list(),
        "online_count": len(manager.active_connections),
        "max_connections": 2
    }

@app.post("/add_device")
async def add_device(device_id: str, nickname: str):
    """手动添加设备（API接口）"""
    if manager.device_manager.register_device(device_id, nickname):
        return {"status": "success", "message": f"设备 {nickname} 已添加"}
    else:
        return {"status": "error", "message": "设备已存在"}

@app.delete("/remove_device/{device_id}")
async def remove_device(device_id: str):
    """删除设备（API接口）"""
    if device_id in manager.device_manager.devices:
        del manager.device_manager.devices[device_id]
        manager.device_manager.save_config()
        return {"status": "success", "message": f"设备 {device_id} 已删除"}
    else:
        return {"status": "error", "message": "设备不存在"}

def get_local_ip():
    """获取本机局域网IP地址"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

if __name__ == "__main__":
    local_ip = get_local_ip()
    
    print("=" * 50)
    print("🚀 启动简·简聊天服务器")
    print("=" * 50)
    print("\n📡 服务器地址:")
    print(f"   - 本地访问: http://127.0.0.1:8000")
    print(f"   - 局域网访问: http://{local_ip}:8000")
    print("\n✨ 功能特点:")
    print("   ✅ 两人专属聊天")
    print("   ✅ 离线消息存储")
    print("   ✅ 设备绑定认证")
    print("   ✅ 自动重连优化")
    print("\n📱 设备管理:")
    print(f"   - 当前已注册设备数: {manager.device_manager.get_device_count()}")
    print(f"   - 已注册设备: {manager.device_manager.get_device_id_list()}")
    print("   - 配置文件: device_config.json")
    print("   - API接口: /devices, /add_device, /remove_device")
    print("=" * 50)
    print("\n💡 使用说明:")
    print("   1. 第一个连接的设备会自动注册")
    print("   2. 后续设备需要先添加到 device_config.json 才能连接")
    print("   3. 最多支持2个设备同时在线聊天")
    print("=" * 50)
    
    uvicorn.run(app, host="0.0.0.0", port=8000)