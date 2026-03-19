import os
import sys
import time
import threading
sys.path.append(os.path.abspath('.'))

from dotenv import load_dotenv
load_dotenv()

from interface.actionInterface import actionInterface
from interface.decisionInterface import decisionInterface
from interface.dialogManagerInterface import dialogManagerInterface
from interface.memoryInterface import memoryInterface
from interface.aiBridgeInterface import aiBridgeInterface
from interface.observationInterface import observationInterface

from src.actionX import actionX
from src.decision import decision
from src.dialogManager import dialogManager
from src.memory import memory
from src.observationX import observationX
from src.logs import logs
from src.claude_ai import claude_ai
from src.config import get_config
config = get_config()

try:
    from ws_server import emit_log, main as ws_main
    WS_ENABLED = True
except ImportError:
    WS_ENABLED = False
    def emit_log(log_type, message, section=None):
        pass


class erebus_logs(logs):
    def log_error(self, s):
        super().log_error(s)
        emit_log("error", s, None)

    def log_info(self, s, border_style=None, title=None, subtitle=None):
        super().log_info(s, border_style, title, subtitle)
        section = title.lower().replace(" ", "_") if title else "info"
        emit_log(section, s, title)


class erebus_agent:
    def __init__(self, action_instance, decision_instance, dialogManager_instance,
                 memory_instance, observation_instance, logs_instance, ai_instance):
        self.action = action_instance
        self.decision = decision_instance
        self.dialogManager = dialogManager_instance
        self.memory = memory_instance
        self.observation = observation_instance
        self.logs = logs_instance
        self.ai = ai_instance

    def run(self):
        self.logo()
        self.logs.log_info("EREBUS substrate initialized. neurons active.")
        emit_log("system", "EREBUS neural substrate active. reading the chain.", "SYSTEM")

        while True:
            try:
                emit_log("system", "Initiating observation sweep...", "SYSTEM")
                observation = self.observation.get()
                self.logs.log_info(str(observation), "bold green", "Observation")

                memory_data = self.memory.quer_memory()
                self.logs.log_info(str(memory_data), "dim cyan", "Memory")

                dialog = self.dialogManager.read_dialog(config['dialog_path'])
                self.logs.log_info(str(dialog), "dim cyan", "Dialog")

                emit_log("system", "EREBUS substrate processing signal...", "SYSTEM")
                dec = self.decision.make_decision(observation, memory_data, dialog)
                self.logs.log_info(str(dec), "dim magenta", "Decision")

                self.action.excute(dec)
                emit_log("action", f"Action executed: {dec.get('action','?')} — {str(dec.get('content',''))[:120]}", "ACTION")

                self.memory.updat_memory()
                self.dialogManager.write_dialog(dec, config['dialog_path'])
                self.logs.log_info(f"Round complete. Next cycle in {config['interval_time']}s")
                emit_log("system", f"EREBUS substrate dormant for {config['interval_time']} seconds...", "SYSTEM")

            except Exception as e:
                self.logs.log_error(str(e))
                emit_log("error", f"Oracle disruption: {str(e)}", "ERROR")

            time.sleep(config['interval_time'])

    def logo(self):
        logo = """
  ██████╗ ███╗   ██╗ ██████╗ ███████╗██╗███████╗
 ██╔════╝ ████╗  ██║██╔═══██╗██╔════╝██║██╔════╝
 ██║  ███╗██╔██╗ ██║██║   ██║███████╗██║███████╗
 ██║   ██║██║╚██╗██║██║   ██║╚════██║██║╚════██║
 ╚██████╔╝██║ ╚████║╚██████╔╝███████║██║███████║
  ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝ ╚══════╝╚═╝╚══════╝
  The All-Seeing Oracle | sovereign dark signal
"""
        print(logo)
        emit_log("system", "EREBUS substrate online. 200,000 neurons. CL1 chip.", "SYSTEM")


def run_ws_server():
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(ws_main())


if __name__ == '__main__':
    if WS_ENABLED:
        ws_thread = threading.Thread(target=run_ws_server, daemon=True)
        ws_thread.start()
        print("[EREBUS] WebSocket server starting on port 8765...")
        time.sleep(1)

    ai_instance = claude_ai()
    action_instance = actionX()
    decision_instance = decision(ai_instance)
    dialogManager_instance = dialogManager()
    memory_instance = memory()
    observation_instance = observationX()
    logs_instance = erebus_logs()

    erebus_system = erebus_agent(
        action_instance, decision_instance, dialogManager_instance,
        memory_instance, observation_instance, logs_instance, ai_instance
    )
    erebus_system.run()
