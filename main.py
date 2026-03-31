# main.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from typing import List, Dict, Optional, Set
import uvicorn
import socket
import json
import hashlib
import uuid
from datetime import datetime
from pathlib import Path
from collections import defaultdict

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
    """消息存储类，用于保存离线消息 - 改为按接收者存储"""
    def __init__(self, max_messages_per_device=100):
        self.messages: Dict[str, List[Dict]] = defaultdict(list)  # receiver_device_id -> messages
        self.max_messages_per_device = max_messages_per_device
        self.processed_message_ids: Set[str] = set()  # 已处理的消息ID，防止重复
    
    def add_message(self, from_device: str, from_nickname: str, to_device: str, content: str, message_id: str = None) -> str:
        """添加离线消息（为特定接收者存储）"""
        # 生成消息ID（如果没有提供）
        if not message_id:
            message_id = str(uuid.uuid4())
        
        # 检查是否已处理过这条消息
        if message_id in self.processed_message_ids:
            print(f"⏭️ 跳过重复消息: {message_id}")
            return message_id
        
        # 标记为已处理
        self.processed_message_ids.add(message_id)
        
        message = {
            "message_id": message_id,
            "from_device": from_device,
            "from_nickname": from_nickname,
            "to_device": to_device,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "time_str": datetime.now().strftime("%H:%M")
        }
        
        self.messages[to_device].append(message)
        
        # 限制每个接收者的最大消息数
        if len(self.messages[to_device]) > self.max_messages_per_device:
            removed = self.messages[to_device].pop(0)
            # 清理过期的消息ID
            if removed.get("message_id") in self.processed_message_ids:
                self.processed_message_ids.remove(removed.get("message_id"))
        
        print(f"📦 保存离线消息: 从 {from_nickname} 到 {to_device} - {content[:20]}... (ID: {message_id})")
        return message_id
    
    def get_messages_for_device(self, device_id: str) -> List[Dict]:
        """获取指定设备的所有离线消息"""
        messages = self.messages.get(device_id, []).copy()
        # 清空该设备的离线消息
        if device_id in self.messages:
            # 清理消息ID记录
            for msg in self.messages[device_id]:
                if msg.get("message_id") in self.processed_message_ids:
                    self.processed_message_ids.remove(msg.get("message_id"))
            self.messages[device_id] = []
        return messages
    
    def has_messages_for_device(self, device_id: str) -> bool:
        """检查指定设备是否有离线消息"""
        return len(self.messages.get(device_id, [])) > 0
    
    def get_all_pending_messages(self) -> Dict[str, List[Dict]]:
        """获取所有待发送的消息"""
        return dict(self.messages)

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []
        self.connection_device: Dict[WebSocket, str] = {}  # websocket -> device_id
        self.device_connection: Dict[str, WebSocket] = {}  # device_id -> websocket
        self.device_manager = DeviceManager()
        self.message_store = MessageStore()
        self.processed_message_ids: Set[str] = set()  # 全局已处理消息ID

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
        
        # 检查是否有发送给自己的离线消息（对方离线时发送的消息）
        if self.message_store.has_messages_for_device(device_id):
            offline_messages = self.message_store.get_messages_for_device(device_id)
            await websocket.send_json({
                "type": "offline_messages",
                "messages": offline_messages
            })
            print(f"📨 发送离线消息给 [{nickname}]: {len(offline_messages)} 条")
        
        # 获取对方设备ID（如果有的话）
        other_device_id = None
        for conn, dev_id in self.connection_device.items():
            if conn != websocket and dev_id != device_id:
                other_device_id = dev_id
                break
        
        # 检查是否有对方设备发送但未接收的消息
        if other_device_id and self.message_store.has_messages_for_device(other_device_id):
            pending_messages = self.message_store.get_messages_for_device(other_device_id)
            if pending_messages:
                await websocket.send_json({
                    "type": "pending_messages",
                    "messages": pending_messages,
                    "note": "对方离线期间发送的消息"
                })
                print(f"📨 发送待处理消息给 [{nickname}]: {len(pending_messages)} 条")
        
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

    async def send_message(self, message_data: dict, sender: WebSocket):
        """发送消息给另一个设备（修复版：正确处理离线消息时序）"""
        sender_device_id = self.connection_device.get(sender)
        sender_nickname = self.device_manager.get_device_nickname(sender_device_id)
        content = message_data.get("content", "")
        message_id = message_data.get("message_id")
        
        # 如果没有消息ID，生成一个
        if not message_id:
            message_id = str(uuid.uuid4())
        
        # 检查是否已处理过这条消息
        if message_id in self.processed_message_ids:
            print(f"⏭️ 跳过重复消息: {message_id}")
            return
        
        # 标记为已处理
        self.processed_message_ids.add(message_id)
        
        # 查找接收者（排除发送者自己）
        receiver = None
        receiver_device_id = None
        for connection, dev_id in self.connection_device.items():
            if connection != sender:
                receiver = connection
                receiver_device_id = dev_id
                break
        
        if receiver:
            # 对方在线，直接发送给接收者
            try:
                await receiver.send_json({
                    "type": "message",
                    "content": content,
                    "from_device": sender_device_id,
                    "from_nickname": sender_nickname,
                    "message_id": message_id,
                    "timestamp": datetime.now().isoformat(),
                    "time_str": datetime.now().strftime("%H:%M")
                })
                print(f"📤 消息已发送: {sender_nickname} -> {receiver_device_id} (ID: {message_id})")
                
                # 发送确认给发送者
                await sender.send_json({
                    "type": "message_ack",
                    "message_id": message_id,
                    "status": "delivered"
                })
            except Exception as e:
                print(f"发送失败: {e}")
                # 发送失败，保存为离线消息（保存给接收者）
                if receiver_device_id:
                    self.message_store.add_message(sender_device_id, sender_nickname, receiver_device_id, content, message_id)
        else:
            # 对方离线，需要找到对方的设备ID
            # 获取所有已授权的设备
            all_devices = self.device_manager.get_device_id_list()
            
            # 找到对方设备（不是发送者自己的设备）
            other_device_id = None
            for dev_id in all_devices:
                if dev_id != sender_device_id:
                    other_device_id = dev_id
                    break
            
            if other_device_id:
                # 保存消息给接收者
                self.message_store.add_message(sender_device_id, sender_nickname, other_device_id, content, message_id)
                print(f"📦 对方离线，消息已保存: {sender_nickname} -> {other_device_id} (ID: {message_id})")
                
                # 发送确认给发送者（表示消息已保存）
                await sender.send_json({
                    "type": "message_ack",
                    "message_id": message_id,
                    "status": "saved_for_offline"
                })
            else:
                # 没有找到对方设备（可能是第一个设备）
                print(f"⚠️ 未找到接收设备，消息暂存: {sender_nickname}")
                # 仍然保存消息，标记为待发送
                self.message_store.add_message(sender_device_id, sender_nickname, "pending", content, message_id)

app = FastAPI(title="简·简")
templates = Jinja2Templates(directory="templates")

manager = ConnectionManager()

@app.get("/", response_class=HTMLResponse)
async def get(request: Request):
    return templates.TemplateResponse("chat.html", {"request": request})

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket端点 - 修复版本"""
    await websocket.accept()
    
    try:
        # 接收设备信息
        device_info = await websocket.receive_text()
        device_data = json.loads(device_info)
        device_id = device_data.get("device_id")
        nickname = device_data.get("nickname", "用户")
        
        if not device_id:
            await websocket.close(code=1008, reason="缺少设备ID")
            return
        
        # 连接处理
        success = await manager.connect(websocket, device_id, nickname)
        if not success:
            return
        
        # 消息循环
        while True:
            data = await websocket.receive_text()
            try:
                message_data = json.loads(data)
                if message_data.get("type") == "message":
                    content = message_data.get("content", "")
                    if content:
                        await manager.send_message(message_data, websocket)
            except json.JSONDecodeError:
                # 纯文本消息（兼容旧版）
                if data.strip():
                    await manager.send_message({"content": data}, websocket)
                
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket错误: {e}")
        try:
            if websocket.client_state.value == 1:
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

@app.get("/messages/pending")
async def get_pending_messages():
    """获取所有待发送消息"""
    return {
        "pending_messages": manager.message_store.get_all_pending_messages()
    }

@app.get("/config")
async def get_config():
    """获取配置信息"""
    return {
        "registered_devices": manager.device_manager.get_device_id_list(),
        "online_count": len(manager.active_connections),
        "max_connections": 2,
        "pending_messages": manager.message_store.get_all_pending_messages()
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
        s.connect(("0.0.0.0", 9000))
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
    print(f"   - 本地访问: http://127.0.0.1:9000")
    print(f"   - 局域网访问: http://{local_ip}:9000")
    print("\n✨ 功能特点:")
    print("   ✅ 两人专属聊天")
    print("   ✅ 离线消息按接收者存储")
    print("   ✅ 设备绑定认证")
    print("   ✅ 自动重连优化")
    print("   ✅ 消息不会重复发送")
    print("   ✅ 发送者不会收到自己的离线消息")
    print("   ✅ 消息ID去重机制")
    print("   ✅ 修复时序问题：发送方先上线也能收到消息")
    print("\n📱 设备管理:")
    print(f"   - 当前已注册设备数: {manager.device_manager.get_device_count()}")
    print(f"   - 已注册设备: {manager.device_manager.get_device_id_list()}")
    print("   - 配置文件: device_config.json")
    print("   - API接口: /devices, /add_device, /remove_device, /messages/pending")
    print("\n📨 离线消息状态:")
    pending = manager.message_store.get_all_pending_messages()
    if pending:
        print(f"   - 有待发送消息: {len(pending)} 个接收者有未读消息")
    else:
        print("   - 无待发送消息")
    print("=" * 50)
    print("\n💡 使用说明:")
    print("   1. 第一个连接的设备会自动注册")
    print("   2. 后续设备需要先添加到 device_config.json 才能连接")
    print("   3. 最多支持2个设备同时在线聊天")
    print("   4. 消息按接收者存储，确保正确送达")
    print("   5. 修复了时序问题：即使发送方先上线，接收方也能收到消息")
    print("   6. 每条消息都有唯一ID，防止重复显示")
    print("=" * 50)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000)
