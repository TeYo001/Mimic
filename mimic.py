from pynput import mouse, keyboard
from time import sleep, time_ns
import queue
from enum import Enum
import sys

# to stop the listener from within, simply return False

class RecordType(Enum):
    MOUSE_MOVE = 0
    MOUSE_CLICK = 1
    MOUSE_SCROLL = 2
    KEY_PRESS = 3
    KEY_RELEASE = 4

class RecordEvent:
    def __init__(self, type, data, timestamp):
        self.type = type
        self.data = data
        self.timestamp = timestamp

    def replay(self, mouse_controller: mouse.Controller, keyboard_controller: keyboard.Controller):
        match self.type:
            case RecordType.MOUSE_MOVE:
                curr_x, curr_y = mouse_controller.position
                new_x, new_y = self.data
                dx = curr_x - new_x
                dy = curr_y - new_y
                mouse_controller.position = (new_x, new_y)
            case RecordType.MOUSE_CLICK:
                mouse_controller.click(self.data)
            case RecordType.MOUSE_SCROLL:
                dx, dy = self.data
                mouse_controller.scroll(dx, dy)
            case RecordType.KEY_PRESS:
                keyboard_controller.press(self.data)
            case RecordType.KEY_RELEASE:
                keyboard_controller.release(self.data)

    def to_save_str(self) -> str:
        save_str = ""
        match self.type:
            case RecordType.MOUSE_MOVE:
                x,y = self.data
                save_str = "MM" + str(x) + "," + str(y)
            case RecordType.MOUSE_CLICK:
                save_str = "MC" + self.data.name
            case RecordType.MOUSE_SCROLL:
                dx, dy = self.data
                save_str = "MS" + str(dx) + "," + str(dy)
            case RecordType.KEY_PRESS:
                save_str = "KP" + str(self.data.vk)
            case RecordType.KEY_RELEASE:
                save_str = "KR" + str(self.data.vk)
        save_str += ";" + str(self.timestamp) + "\n"
        return save_str

class State(Enum):
    # transition states
    IDLE = 0
    RECORDING = 1
    REPLAYING = 2
    SAVING = 4
    INVALID = 5
    EXITING = 6
    TYPING = 7
    WAIT_TIME = 8
    AWAIT_PRESS = 9
    REPEAT = 10

class ActionType(Enum):
    SPECIAL_KEY = 0
    COMMAND = 1
    COMMAND_SEQUENCE = 2

class Action:
    def __init__(self, type: ActionType, state: State, must_finish: bool = False, extra_data=None):
        self.type = type
        self.state = state
        self.must_finish = must_finish
        self.extra_data = extra_data

class StateMachine:
    def __init__(self, function_table: dict):
        self.state = State.IDLE
        self.record_filename = "save.txt"
        self.record = list()
        self.current_record_idx = 0
        self.base_record_time = 0
        self.function_table = function_table
        self.running = False
    
    def run(self):
        global action_q, low_prio_action_q, global_flags
        self.running = True
        # blocks until an action is available
        while self.running:
            if action_q.empty() and not low_prio_action_q.empty():
                self.do(low_prio_action_q.get(block=True, timeout=1))
            elif global_flags[GlobalFlag.IS_DAEMON]:
                action = action_q.get(block=True)
                self.do(action)
            else:
                if action_q.empty():
                    return
                action = action_q.get(block=True, timeout=1)
                self.do(action)

    def do(self, action: Action):
        global global_flags
        if action.state == State.EXITING:
            self.running = False
            return
        if action.state == State.IDLE and not global_flags[GlobalFlag.IS_DAEMON]:
            self.running = False
            return
        self.state = action.state
        start_func, run_func, stop_func = self.function_table.get(action.state)
        if not start_func is None:
            start_func(self, action)
        if run_func is None:
            print("ERROR: run_func is not allowed to be None")
            exit(1)
        while True:
            if not action.must_finish and not action_q.empty():
                break
            elif self.state == State.IDLE and not low_prio_action_q.empty():
                break
            new_state = run_func(self, action)
            if not new_state is None:
                self.state = new_state
                break
        if not stop_func is None:
            stop_func(self, action)

class GlobalFlag(Enum):
    IS_DAEMON = 0,
    USE_SPECIAL_KEYS = 1,
    FORCE_MUST_FINISH = 2,

def record_event_from_save_str(save_str: str) -> RecordEvent:
    type = None
    data = None
    timestamp = None
    type_str = save_str[0:2]
    split = save_str.split(';')
    timestamp = int(split[1])
    split = split[0][2:]
    match type_str:
        case "MM": 
            type = RecordType.MOUSE_MOVE       
            parts = split.split(',')
            data = (int(parts[0]), int(parts[1]))
        case "MC": 
            type = RecordType.MOUSE_CLICK
            match split:
                case "left": data = mouse.Button.left
                case "middle": data = mouse.Button.middle
                case "right": data = mouse.Button.right
                case _:
                    print("ERROR: No clue what button this is")
                    exit(1)
        case "MS": 
            type = RecordType.MOUSE_SCROLL
            parts = split.split(',')
            data = (int(parts[0]), int(parts[1]))
        case "KP": 
            type = RecordType.KEY_PRESS
            data = keyboard.KeyCode.from_vk(int(split))
        case "KR": 
            type = RecordType.KEY_RELEASE
            data = keyboard.KeyCode.from_vk(int(split))
    return RecordEvent(type, data, timestamp)

MAX_RECORD_SIZE = 4096
START_RECORD_KEY = keyboard.Key.home
START_REPLAY_KEY = keyboard.Key.up
SAVE_REPLAY_KEY = keyboard.Key.down
STOP_RUNNING_KEY = keyboard.Key.delete
START_IDLE_KEY = keyboard.Key.end
IGNORE_KEYS = [START_RECORD_KEY, START_REPLAY_KEY, SAVE_REPLAY_KEY, STOP_RUNNING_KEY, START_IDLE_KEY]

### GLOBALS ###
event_out_q = queue.Queue(MAX_RECORD_SIZE)
action_q = queue.Queue(10)
low_prio_action_q = queue.Queue(128)
await_press_key = None

### GLOBAL FLAGS ###
global_flags = {
    GlobalFlag.IS_DAEMON : False, # I use the term daemon to refer to a program that doesn't stop running even if there are no more tasks to do
    GlobalFlag.USE_SPECIAL_KEYS : False,
    GlobalFlag.FORCE_MUST_FINISH : False
}

def on_move(x: int, y: int) -> bool | None:
    event_out_q.put(RecordEvent(RecordType.MOUSE_MOVE, (x, y), time_ns()))

def on_click(x: int, y: int, button: mouse.Button, pressed: bool) -> bool | None:
    event_out_q.put(RecordEvent(RecordType.MOUSE_CLICK, button, time_ns()))

def on_scroll(x: int, y: int, dx: int, dy: int) -> bool | None:
    event_out_q.put(RecordEvent(RecordType.MOUSE_SCROLL, (dx, dy), time_ns()))

def on_press(key: keyboard.Key) -> bool | None:
    if key in IGNORE_KEYS:
        return
    event_out_q.put(RecordEvent(RecordType.KEY_PRESS, key, time_ns()))

def on_release(key: keyboard.Key) -> bool | None:
    if key in IGNORE_KEYS:
        return
    event_out_q.put(RecordEvent(RecordType.KEY_RELEASE, key, time_ns()))

def on_special_press(key: keyboard.Key) -> bool | None:
    global await_press_key, global_flags
    if not global_flags[GlobalFlag.IS_DAEMON]:
        if key == START_RECORD_KEY:
            action_q.put(Action(ActionType.SPECIAL_KEY, State.RECORDING))
        elif key == START_REPLAY_KEY:
            action_q.put(Action(ActionType.SPECIAL_KEY, State.REPLAYING))
        elif key == SAVE_REPLAY_KEY:
            action_q.put(Action(ActionType.SPECIAL_KEY, State.SAVING, must_finish=True))
    if key == START_IDLE_KEY:
        action_q.put(Action(ActionType.SPECIAL_KEY, State.IDLE))
    elif key == STOP_RUNNING_KEY:
        action_q.put(Action(ActionType.SPECIAL_KEY, State.EXITING, must_finish=True))
    elif key == await_press_key:
        action_q.put(Action(ActionType.COMMAND, State.IDLE))

def save_recording_to_file(filename: str, event_q: queue.Queue):
    file_str = ""
    print(event_q.qsize())
    while event_q.qsize() != 0:
        record_event = event_q.get()
        file_str += record_event.to_save_str()
    
    print(file_str)
    with open(filename, "w") as file:
        file.write(file_str)

def read_recording_from_file(filename: str, out_array: list):
    with open(filename, "r") as file:
        for save_str in file.readlines():
            out_array.append(record_event_from_save_str(save_str))    

def idle(sm: StateMachine, action: Action) -> State | None:
    sleep(0.01)

def start_recording(mouse_listener: mouse.Listener, keyboard_listener: keyboard.Listener):
    print("start recording")
    mouse_listener.start()
    keyboard_listener.start()

def stop_recording(mouse_listener: mouse.Listener, keyboard_listener: keyboard.Listener):
    print("stop recording")
    mouse_listener.stop()
    keyboard_listener.stop()

def init_replaying(sm: StateMachine, action: Action):
    if not action.extra_data is None:
        sm.record_filename = action.extra_data
    read_recording_from_file(sm.record_filename, sm.record)
    sm.current_record_idx = 0
    sm.base_record_time = time_ns()
    base_time = sm.record[0].timestamp
    for record_event in sm.record:
        record_event.timestamp -= base_time

def run_replaying(sm: StateMachine, action: Action, mouse_controller: mouse.Controller, keyboard_controller: keyboard.Controller):
    if sm.current_record_idx >= len(sm.record):
        return State.IDLE
    print(sm.record[sm.current_record_idx].timestamp)
    while time_ns() < sm.record[sm.current_record_idx].timestamp:
        continue
    sm.record[sm.current_record_idx].replay(mouse_controller, keyboard_controller)
    sm.current_record_idx += 1

def save_recording(sm: StateMachine, action: Action):
    if not action.extra_data is None:
        sm.record_filename = action.extra_data
    save_recording_to_file(sm.record_filename, event_out_q)
    return State.IDLE

def init_typing(sm: StateMachine, action: Action):
    sm.current_record_idx = 0 # typing reuses this for simplicity

def run_typing(sm: StateMachine, action: Action, keyboard_controller: keyboard.Controller):
    message = action.extra_data
    if sm.current_record_idx >= len(message):
        return State.IDLE
    keyboard_controller.press(message[sm.current_record_idx])
    keyboard_controller.release(message[sm.current_record_idx])
    sm.current_record_idx += 1

def run_wait_time(sm: StateMachine, action: Action):
    sleep(action.extra_data)
    return State.IDLE

def init_await_press(sm: StateMachine, action: Action):
    global await_press_key
    await_press_key = action.extra_data

def init_repeat(sm: StateMachine, action: Action):
    for action in action.extra_data:
        low_prio_action_q.put(action)

def run_repeat(sm: StateMachine, action: Action):
    return State.IDLE

def parse_record_command(idx: int, cmds: list[str], action_list: list[Action]) -> int:
    action_list.append(Action(ActionType.COMMAND, State.RECORDING, False))
    return idx + 1

def parse_replay_command(idx: int, cmds: list[str], action_list: list[Action]) -> int:
    if (idx + 1 == len(cmds)):
        print("ERROR: replay command requires the replay file")
        exit(1)
    replay_filename = cmds[idx + 1]
    action_list.append(Action(ActionType.COMMAND, State.REPLAYING, False, replay_filename))
    return idx + 2

def parse_type_command(idx: int, cmds: list[str], action_list: list[Action]) -> int:
    if (idx + 1 == len(cmds)):
        print("ERROR: type command requires the message to type")
        exit(1)
    message = cmds[idx + 1]
    if not (message.startswith("\'") and message.endswith("\'")):
        print("ERROR: type command requires the message to be within apostrophes")
        exit(1)
    action_list.append(Action(ActionType.COMMAND, State.TYPING, False, message[1:len(message)-1]))
    return idx + 2

def parse_wait_time_command(idx: int, cmds: list[str], action_list: list[Action]) -> int:
    if (idx + 1 == len(cmds)):
        print("ERROR: wait_time command requires the time to wait")
        exit(1)
    time_sec = float(cmds[idx + 1])
    action_list.append(Action(ActionType.COMMAND, State.WAIT_TIME, False, time_sec))
    return idx + 2

def parse_await_press_command(idx: int, cmds: list[str], action_list: list[Action]) -> int:
    if (idx + 1 == len(cmds)):
        print("ERROR: await_press command requires the key to press")
        exit(1)
    key_str = cmds[idx + 1]
    key = None
    if len(key_str) == 1:
        key = keyboard.Key(key_str)
    match key_str:
        case "ctrl_l": key = keyboard.Key.ctrl_l
        case "ctrl_r": key = keyboard.Key.ctrl_r
        case "alt_l": key = keyboard.Key.alt_l
        case "alt_r": key = keyboard.Key.alt_r
        case "alt_gr": key = keyboard.Key.alt_gr
        case "shift_l": key = keyboard.Key.shift_l
        case "shift_r": key = keyboard.Key.shift_r
        case "caps_lock": key = keyboard.Key.caps_lock
        case "tab": key = keyboard.Key.tab
        case "space": key = keyboard.Key.space
        case "insert": key = keyboard.Key.insert
        case "delete": key = keyboard.Key.delete
        case "home": key = keyboard.Key.home
        case "end": key = keyboard.Key.end
        case "page_up": key = keyboard.Key.page_up
        case "page_down": key = keyboard.Key.page_down
        case "up": key = keyboard.Key.up
        case "down": key = keyboard.Key.down
        case "left": key = keyboard.Key.left
        case "right": key = keyboard.Key.right
        case "f1": key = keyboard.Key.f1
        case "f2": key = keyboard.Key.f2
        case "f3": key = keyboard.Key.f3
        case "f4": key = keyboard.Key.f4
        case "f5": key = keyboard.Key.f5
        case "f6": key = keyboard.Key.f6
        case "f7": key = keyboard.Key.f7
        case "f8": key = keyboard.Key.f8
        case "f9": key = keyboard.Key.f9
        case "f10": key = keyboard.Key.f10
        case "f11": key = keyboard.Key.f11
        case "f12": key = keyboard.Key.f12
        case _:
            print(f"ERROR: The key: \'{key_str}\' doesn't exist or hasn't been added by me")
            exit(1)
    action_list.append(Action(ActionType.COMMAND, State.AWAIT_PRESS, False, key))
    return idx + 2

def parse_repeat_command(idx: int, cmds: list[str], action_list: list[Action]) -> int:
    if idx + 1 != len(cmds):
        print("WARNING: repeat command should always be the last command in the command list as the commands after it will never be run")
    action = Action(ActionType.COMMAND, State.REPEAT, extra_data=action_list.copy())
    action.extra_data.append(action)
    action_list.append(action)
    return idx + 1

def handle_command_str(cmd_str: str):
    action_list = list()
    cmds = cmd_str.split(' ')
    idx = 0
    while True:
        if idx >= len(cmds):
            break
        cmd = cmds[idx]
        match cmd:
            case "record": idx = parse_record_command(idx, cmds, action_list)
            case "replay": idx = parse_replay_command(idx, cmds, action_list)
            case "type": idx = parse_type_command(idx, cmds, action_list)
            case "wait_time": idx = parse_wait_time_command(idx, cmds, action_list)
            case "await_press": idx = parse_await_press_command(idx, cmds, action_list)
            case "repeat": idx = parse_repeat_command(idx, cmds, action_list)
            case _:
                print(f"ERROR: unknown command: {cmd}")
                exit(1)
    for action in action_list:
        low_prio_action_q.put(action)
            

def handle_flag_str_list():
    str_list = sys.argv
    if len(str_list) == 1:
        global_flags[GlobalFlag.IS_DAEMON] = True
        global_flags[GlobalFlag.USE_SPECIAL_KEYS] = True
    for flag in filter(lambda x: str.startswith(x, "-"), str_list):
        match flag:
            case "-f":
                global_flags[GlobalFlag.FORCE_MUST_FINISH] = True
            case "-d":
                global_flags[GlobalFlag.IS_DAEMON] = True
        if flag.startswith("-c="):
            handle_command_str(flag[len("-c="):])
        else:
            print(f"ERROR: invalid flag {flag}")
            exit(1)

def main():
    handle_flag_str_list()

    mouse_listener = mouse.Listener(
        on_move=on_move,
        on_click=on_click,
        on_scroll=on_scroll)
    
    keyboard_listener = keyboard.Listener(
        on_press=on_press,
        on_release=on_release)
    
    special_listener = keyboard.Listener(
        on_press=on_special_press)
    special_listener.start()

    mouse_controller = mouse.Controller()
    keyboard_controller = keyboard.Controller()
    
    func_table = {
        State.IDLE : (None, idle, None),
        State.RECORDING : (lambda x,y: start_recording(mouse_listener, keyboard_listener), idle, lambda x,y: stop_recording(mouse_listener, keyboard_listener)),
        State.REPLAYING : (init_replaying, lambda x,y: run_replaying(x,y, mouse_controller, keyboard_controller), None),
        State.SAVING : (None, save_recording, None),
        State.TYPING : (init_typing, lambda x,y: run_typing(x,y, keyboard_controller), None),
        State.WAIT_TIME : (None, run_wait_time, None),
        State.AWAIT_PRESS : (init_await_press, idle, None),
        State.REPEAT : (init_repeat, run_repeat, None)
    }
    sm = StateMachine(func_table)
    sm.run()
    special_listener.stop()
    exit(0)

if __name__ == "__main__":
    main()