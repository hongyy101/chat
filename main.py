# main.py
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from typing import List
import uvicorn
import socket

app = FastAPI(title="约.约")
templates = Jinja2Templates(directory="templates")

class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        print(f"新连接加入，当前连接数: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            print(f"连接断开，当前连接数: {len(self.active_connections)}")

    async def broadcast(self, message: str, sender: WebSocket):
        """广播消息给所有客户端"""
        disconnected = []
        for connection in self.active_connections:
            try:
                is_self = (connection == sender)
                await connection.send_json({
                    "message": message,
                    "isSelf": is_self
                })
            except Exception as e:
                print(f"发送消息失败: {e}")
                disconnected.append(connection)
        
        # 清理断开的连接
        for conn in disconnected:
            self.disconnect(conn)

manager = ConnectionManager()

@app.get("/", response_class=HTMLResponse)
async def get():
    return templates.TemplateResponse("chat.html", {"request": {}})

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # 接收消息
            data = await websocket.receive_text()
            print(f"收到消息: {data}")
            # 广播给所有客户端
            await manager.broadcast(data, websocket)
    except WebSocketDisconnect:
        print("WebSocket正常断开")
        manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket错误: {e}")
        manager.disconnect(websocket)

def get_local_ip():
    """获取本机局域网IP地址"""
    try:
        # 创建一个UDP连接获取本机IP
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
    print("🚀 启动极简微信聊天服务器")
    print("=" * 50)
    print("\n📡 服务器地址:")
    print(f"   - 本地访问: http://127.0.0.1:8000")
    print(f"   - 局域网访问: http://{local_ip}:8000")
    print("\n📱 手机访问步骤:")
    print("   1. 确保手机和电脑连接同一个WiFi")
    print("   2. 在手机浏览器输入上面的局域网地址")
    print("   3. 如果无法访问，请检查防火墙设置")
    print("=" * 50)
    
    # 使用 "0.0.0.0" 监听所有网络接口
    uvicorn.run(app, host="0.0.0.0", port=8000)