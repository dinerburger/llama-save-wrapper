import asyncio
import sys
import signal
import os
import random
import argparse
from dotenv import load_dotenv
from aiohttp import web, ClientSession, ClientConnectorError

load_dotenv()

# --- Configuration ---
BINARY_PATH = os.environ.get("LLAMA_BINARY", "/usr/local/bin/llama-server")
SLOTS = [0, 1, 2, 3]

class LlamaGatekeeper:
    def __init__(self, public_port: int, extra_args: list):
        self.public_port = public_port
        self.extra_args = extra_args
        # Use a random port in the ephemeral range for the backend
        self.backend_port = random.randint(49152, 65535)
        self.backend_url = f"http://localhost:{self.backend_port}"

        # Extract --slot-save-path for later use
        self.save_path = None
        if "--slot-save-path" in extra_args:
            idx = extra_args.index("--slot-save-path")
            if idx + 1 < len(extra_args):
                self.save_path = extra_args[idx + 1]

        self.process = None
        self.is_ready = False  # The "Gate" - False until restore_slots is done
        self.session = None
        self._shutting_down = False
        self._force_quit = False

    async def wait_for_health(self):
        print(f"✨ Waiting for llama-server to be healthy on internal port {self.backend_port}...")
        while True:
            try:
                async with self.session.get(f"{self.backend_url}/health") as resp:
                    if resp.status == 200:
                        print("✅ Backend server is healthy!")
                        return
            except (ClientConnectorError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(1)

    async def restore_slots(self):
        print("🔄 Restoring slot KV caches...")
        for slot_id in SLOTS:
            filename = f"{slot_id}.bin"
            if self.save_path:
                filepath = os.path.join(self.save_path, filename)
                if not os.path.exists(filepath):
                    print(f"  - Slot {slot_id}: Skipped (no {filename})")
                    continue
            try:
                # POST /slots/{id}?action=restore
                async with self.session.post(
                    f"{self.backend_url}/slots/{slot_id}?action=restore", 
                    json={"filename": filename}
                ) as resp:
                    if resp.status == 200:
                        print(f"  - Slot {slot_id}: Restored {filename}")
                    else:
                        print(f"  - Slot {slot_id}: Failed to restore ({resp.status})")
            except Exception as e:
                print(f"  - Slot {slot_id}: Error during restore: {e}")
        
        self.is_ready = True
        print("🔓 Gate opened! /health is now returning 200 OK.")

    async def save_slots(self):
        if not self.is_ready:
            print("⚠️ Backend never became ready, skipping save.")
            return
        print("💾 Signal received! Saving slot KV caches before exit...")
        for slot_id in SLOTS:
            if self._force_quit:
                print("⚠️ Force quit requested, aborting save.")
                return
            filename = f"{slot_id}.bin"
            try:
                async with self.session.post(
                    f"{self.backend_url}/slots/{slot_id}?action=save", 
                    json={"filename": filename}
                ) as resp:
                    if resp.status == 200:
                        print(f"  - Slot {slot_id}: Saved {filename}")
                    else:
                        print(f"  - Slot {slot_id}: Failed to save ({resp.status})")
            except Exception as e:
                print(f"  - Slot {slot_id}: Error during save: {e}")

    async def proxy_handler(self, request):
        """Handles all incoming requests and forwards them to the backend."""
        # SPECIAL CASE: The Health Gate
        if request.path == "/health" or request.path == "/v1/health":
            if not self.is_ready:
                # Return 503 to llama-swap even if the backend is actually 200
                return web.Response(status=503, text="Loading/Restoring")
            
            # If ready, just proxy the actual health check from the backend
            async with self.session.get(f"{self.backend_url}{request.path}") as resp:
                return web.Response(status=resp.status, text=await resp.text())

        # GENERAL CASE: Proxy everything else
        method = request.method
        url = f"{self.backend_url}{request.path}"
        if request.query_string:
            url += f"?{request.query_string}"
        
        headers = {k: v for k, v in request.headers.items() if k.lower() != 'host'}
        body = await request.read()

        try:
            async with self.session.request(method, url, headers=headers, data=body) as resp:
                content_type = resp.headers.get("Content-Type", "")
                if "text/event-stream" in content_type or "application/x-ndjson" in content_type:
                    # Streaming response — forward chunks as they arrive
                    response = web.StreamResponse(status=resp.status)
                    await response.prepare(request)
                    async for chunk in resp.content.iter_any():
                        await response.write(chunk)
                    return response
                else:
                    # Non-streaming response — return full body
                    return web.Response(
                        status=resp.status,
                        headers={k: v for k, v in resp.headers.items() if k.lower() != "transfer-encoding"},
                        body=await resp.read(),
                    )
        except Exception as e:
            return web.Response(status=502, text=f"Bad Gateway: {e}")

    async def run(self):
        # --- Directory Assurance ---
        if self.save_path and not os.path.exists(self.save_path):
            try:
                print(f"📁 Creating missing save path: {self.save_path}")
                os.makedirs(self.save_path, exist_ok=True)
            except OSError as e:
                print(f"⚠️ Warning: Could not create slot-save-path: {e}")

        # 1. Start the backend server
        # Update extra_args to use our internal port
        # We replace --port if it exists, or add it
        cmd_args = self.extra_args[:]
        if "--port" in cmd_args:
            idx = cmd_args.index("--port")
            cmd_args[idx+1] = str(self.backend_port)
        else:
            cmd_args.extend(["--port", str(self.backend_port)])

        self.process = await asyncio.create_subprocess_exec(
            "stdbuf", "-oL", "-eL", BINARY_PATH, *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        print(f"🚀 Started llama-server on internal port {self.backend_port} (PID: {self.process.pid})")

        # Echo subprocess stdout/stderr to the correct channels
        async def pipe_stream(stream, target):
            while True:
                line = await stream.readline()
                if not line:
                    break
                target.write(line)

        asyncio.create_task(pipe_stream(self.process.stdout, sys.stdout.buffer))
        asyncio.create_task(pipe_stream(self.process.stderr, sys.stderr.buffer))

        # 2. Setup Session and Proxy Server
        self.session = ClientSession()
        app = web.Application()
        app.router.add_route('*', '/{tail:.*}', self.proxy_handler)
        
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, 'localhost', self.public_port)
        await site.start()
        print(f"🌐 Proxy listening on port {self.public_port}...")

        # 3. Orchestrate restoration
        await self.wait_for_health()
        await self.restore_slots()

        print("🏃 Wrapper is now active and proxying traffic. Use Ctrl+C to stop and save.")
        
        # Keep the loop running until the process is terminated
        try:
            while self.process.returncode is None:
                await asyncio.sleep(1)
        finally:
            await self.session.close()
            await runner.cleanup()

    def handle_exit(self, signum, frame):
        if self._shutting_down:
            print("\n⚠️ Second interrupt received — force quitting without saving!")
            self._force_quit = True
            return
        self._shutting_down = True
        asyncio.create_task(self.shutdown())

    async def shutdown(self):
        if self._force_quit:
            print("⚠️ Force quit! Skipping save, terminating immediately.")
        else:
            await self.save_slots()
        if self.process:
            print("🛑 Terminating llama-server...")
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except ProcessLookupError:
                pass  # already gone
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        
        print("👋 Bye-bye!")
        os._exit(0)

async def main():
    parser = argparse.ArgumentParser(description="Llama-server Gatekeeper Proxy")
    args, unknown = parser.parse_known_args()

    public_port = None
    for i, arg in enumerate(sys.argv):
        if arg == "--port" and i + 1 < len(sys.argv):
            public_port = int(sys.argv[i+1])
            break

    if public_port is None:
        print("❌ Error: --port xxxxx must be provided for the public proxy.")
        sys.exit(1)

    gatekeeper = LlamaGatekeeper(public_port=public_port, extra_args=sys.argv[1:])
    
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, lambda: gatekeeper.handle_exit(None, None))

    await gatekeeper.run()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

