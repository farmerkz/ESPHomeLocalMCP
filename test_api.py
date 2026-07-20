import asyncio
import websockets
import json

async def main():
    async with websockets.connect('ws://127.0.0.1:6052/ws') as ws:
        hello = await ws.recv()
        print("Hello:", hello)
        
        # devices/validate
        await ws.send(json.dumps({"command": "devices/validate", "message_id": "1", "args": {"configuration": "test.yaml"}}))
        while True:
            res = await ws.recv()
            data = json.loads(res)
            print("Response:", data)
            if data.get("message_id") == "1" and data.get("event") == "result":
                break

asyncio.run(main())
