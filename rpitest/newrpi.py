#!/usr/bin/env python3

import io
import json
import queue
import time
from multiprocessing import Process, Manager
from typing import Optional, List

import os
#import picamera
import requests

from android import AndroidLink, AndroidMessage
from stm32 import STMLink
from consts import SYMBOL_MAP
from logger import prepare_logger
from settings import API_IP, API_PORT, OUTDOOR_BIG_TURN


class PiAction:
    """
    Represents an action that the Pi is responsible for:
    - Changing the robot's mode (manual/path)
    - Requesting a path from the API
    - Snapping an image and requesting the image-rec result from the API
    """

    def __init__(self, cat, value):
        self._cat = cat
        self._value = value

    @property
    def cat(self):
        return self._cat

    @property
    def value(self):
        return self._value


class RaspberryPi:
    def __init__(self):
        # prepare logger
        self.logger = prepare_logger()

        # communication links
        self.android_link = AndroidLink()
        self.stm_link = STMLink()

        # for sharing information between child processes
        manager = Manager()

        # 0: manual, 1: path (default: 1)
        self.robot_mode = manager.Value('i', 0)

        # events
        self.android_dropped = manager.Event()  # set when the android link drops
        self.unpause = manager.Event()  # commands will be retrieved from commands queue when this event is set

        # movement lock, commands will only be sent to STM32 if this is released
        self.movement_lock = manager.Lock()

        # queues
        self.android_queue = manager.Queue() # Messages to send to Android
        self.rpi_action_queue = manager.Queue()# Messages that need to be processed by RPi
        self.command_queue = manager.Queue()# Messages that need to be processed by STM32, as well as snap commands
        self.path_queue = manager.Queue()# X,Y,D coordinates of the robot after execution of a command

        # define processes
        self.proc_recv_android = None
        self.proc_recv_stm32 = None
        self.proc_android_sender = None
        self.proc_command_follower = None
        self.proc_rpi_action = None

    def start(self):
        try:
            # establish bluetooth connection with Android
            self.android_link.connect()
            self.android_queue.put(AndroidMessage('info', 'You are connected to the RPi!'))

            # establish connection with STM32
            self.stm_link.connect()

            # check api status
            self.check_api()

            # define processes
            self.proc_recv_android = Process(target=self.recv_android)
            self.proc_recv_stm32 = Process(target=self.recv_stm)
            self.proc_android_sender = Process(target=self.android_sender)
            self.proc_command_follower = Process(target=self.command_follower)
            self.proc_rpi_action = Process(target=self.rpi_action)

            # start processes
            self.proc_recv_android.start()
            self.proc_recv_stm32.start()
            self.proc_android_sender.start()
            self.proc_command_follower.start()
            self.proc_rpi_action.start()

            self.logger.info("Child Processes started")
            self.android_queue.put(AndroidMessage('info', 'Robot is ready!'))
            self.android_queue.put(AndroidMessage('mode', 'path' if self.robot_mode.value == 1 else 'manual'))

            # buzz STM32 (2 times)
            self.stm_link.send("ZZ02")

            # reconnect handler to watch over android connection
            #self.reconnect_android()

        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        #self.android_link.disconnect()
        self.stm_link.disconnect()
        self.logger.info("Program exited!")

    def reconnect_android(self):
        self.logger.info("Reconnection handler is watching...")

        while True:
            # wait for android connection to drop
            self.android_dropped.wait()

            self.logger.error("Android link is down!")

            # buzz STM32 (3 times)
            self.stm_link.send("ZZ03")

            # kill child processes
            self.logger.debug("Killing android child processes")
            self.proc_android_sender.kill()
            self.proc_recv_android.kill()

            # wait for the child processes to finish
            self.proc_android_sender.join()
            self.proc_recv_android.join()
            assert self.proc_android_sender.is_alive() is False
            assert self.proc_recv_android.is_alive() is False
            self.logger.debug("Android child processes killed")

            # clean up old sockets
            self.android_link.disconnect()

            # reconnect
            self.android_link.connect()

            # recreate android processes
            self.proc_recv_android = Process(target=self.recv_android)
            self.proc_android_sender = Process(target=self.android_sender)

            # start processes
            self.proc_recv_android.start()
            self.proc_android_sender.start()

            self.logger.info("Android child processes restarted")
            self.android_queue.put(AndroidMessage("info", "You are reconnected!"))
            self.android_queue.put(AndroidMessage('mode', 'path' if self.robot_mode.value == 1 else 'manual'))

            # buzz STM32 (2 times)
            self.stm_link.send("ZZ02")

            self.android_dropped.clear()

    def recv_android(self) -> None:
        while True:
            msg_str: Optional[str] = None
            try:
                msg_str = self.android_link.recv()
            except OSError:
                self.android_dropped.set()
                self.logger.debug("Event set: Android connection dropped")

            # if an error occurred in recv()
            if msg_str is None:
                continue

            message: dict = json.loads(msg_str)

            # change mode command
            if message['cat'] == "mode":
                self.rpi_action_queue.put(PiAction(**message))
                self.logger.debug(f"Change mode PiAction added to queue: {message}")

            # manual movement commands
            elif message['cat'] == "manual":
                if self.robot_mode.value == 0:  # robot must be in manual mode
                    self.command_queue.put(message['value'])
                    self.logger.debug(f"Manual Movement added to command queue: {message['value']}")
                else:
                    self.android_queue.put(AndroidMessage("error", "Manual movement not allowed in Path mode."))
                    self.logger.warning("Manual movement not allowed in Path mode.")

            # set obstacles
            elif message['cat'] == "obstacles":
                if self.robot_mode.value == 1:  # robot must be in path mode
                    self.rpi_action_queue.put(PiAction(**message))
                    self.logger.debug(f"Set obstacles PiAction added to queue: {message}")
                else:
                    self.android_queue.put(AndroidMessage("error", "Robot must be in Path mode to set obstacles."))
                    self.logger.warning("Robot must be in Path mode to set obstacles.")

            # control commands
            elif message['cat'] == "control":
                if message['value'] == "start":
                    # robot must be in path mode
                    if self.robot_mode.value == 1:
                        # check api
                        if not self.check_api():
                            self.logger.error("API is down! Start command aborted.")
                            self.android_queue.put(AndroidMessage('error', "API is down, start command aborted."))

                            # buzz STM32 (4 times)
                            self.stm_link.send("ZZ04")

                        # commencing path following
                        if not self.command_queue.empty():
                            self.unpause.set()
                            self.logger.info("Start command received, starting robot on path!")
                            self.android_queue.put(AndroidMessage('info', 'Starting robot on path!'))
                            self.android_queue.put(AndroidMessage('status', 'running'))
                        else:
                            self.logger.warning("The command queue is empty, please set obstacles.")
                            self.android_queue.put(
                                AndroidMessage("error", "Command queue is empty, did you set obstacles?"))
                    else:
                        self.android_queue.put(
                            AndroidMessage("error", "Robot must be in Path mode to start robot on path."))
                        self.logger.warning("Robot must be in Path mode to start robot on path.")

            # navigate around obstacle
            elif message['cat'] == "single-obstacle":
                if self.robot_mode.value == 1:  # robot must be in path mode
                    self.rpi_action_queue.put(PiAction(**message))
                    self.logger.debug(f"Single-obstacle PiAction added to queue: {message}")
                else:
                    self.android_queue.put(
                        AndroidMessage("error", "Robot must be in Path mode to set single obstacle."))
                    self.logger.warning("Robot must be in Path mode to set single obstacle.")

    def recv_stm(self) -> None:
        """
        Receive acknowledgement messages from STM32, and release the movement lock
        """
        while True:
            message: str = self.stm_link.recv()

            # acknowledgement from STM32
            if message.startswith("ACK"):
                # release movement lock
                try:
                    self.movement_lock.release()
                    self.logger.debug("ACK from STM32 received, movement lock released.")

                    # if in path mode, get new location and notify android
                    if self.robot_mode.value == 1:
                        temp = self.path_queue.get_nowait()
                        location = {
                            "x": temp['x'],
                            "y": temp['y'],
                            "d": temp['d'],
                        }
                        self.android_queue.put(AndroidMessage('location', location))
                    else:
                        if message == "ACK|X":
                            self.logger.debug("Fastest car ACK received from STM32!")
                            self.android_queue.put(AndroidMessage("info", "Robot has completed fastest car!"))
                            self.android_queue.put(AndroidMessage("status", "finished"))
                except Exception:
                    self.logger.warning("Tried to release a released lock!")
            else:
                self.logger.warning(f"Ignored unknown message from STM: {message}")

    def android_sender(self) -> None:
        """
        Responsible for retrieving messages from the outgoing message queue and sending them over the Android Link
        """
        while True:
            # retrieve from queue
            try:
                message: AndroidMessage = self.android_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            # send it over the android link
            try:
                self.android_link.send(message)
            except OSError:
                self.android_dropped.set()
                self.logger.debug("Event set: Android dropped")

    def command_follower(self) -> None:
        while True:
            # retrieve next movement command
            command: str = self.command_queue.get()

            # wait for unpause event to be true
            self.unpause.wait()

            # acquire lock first (needed for both moving, and snapping pictures)
            self.movement_lock.acquire()

            # STM32 commands
            stm32_prefixes = ("FS", "BS", "FW", "BW", "FL", "FR", "BL", "BR", "TL", "TR", "A", "C", "DT", "STOP", "ZZ")
            if command.startswith(stm32_prefixes):
                self.stm_link.send(command)

            elif command.startswith("WN"):
                self.stm_link.send(command)
                self.android_queue.put(AndroidMessage('status', 'running'))
                self.android_queue.put(AndroidMessage("info", "Starting robot on fastest car!"))

            # snap command (path mode)
            elif command.startswith("SNAP"):
                obstacle_id = command.replace("SNAP", "")
                self.rpi_action_queue.put(PiAction(cat="snap", value=obstacle_id))

            # snap command (manual mode)
            elif command.startswith("MANSNAP"):
                obstacle_id = "99"
                self.rpi_action_queue.put(PiAction(cat="snap", value=obstacle_id))

            # no-op (a workaround to let the robot stop after a non-bullseye face has been found)
            elif command.startswith("NOOP"):
                # self.stm_link.send("FW00")
                self.movement_lock.release()

            # end of path
            elif command == "FIN":
                self.stm_link.send("ZZ01")
                # clear the unpause event (no new command will be retrieved from queue)
                self.unpause.clear()
                self.movement_lock.release()
                self.logger.info("Commands queue finished.")
                self.android_queue.put(AndroidMessage("info", "Commands queue finished."))
                self.android_queue.put(AndroidMessage("status", "finished"))
                self.rpi_action_queue.put(PiAction(cat="stitch", value=""))
            else:
                raise Exception(f"Unknown command: {command}")

    def rpi_action(self):
        while True:
            action: PiAction = self.rpi_action_queue.get()
            self.logger.debug(f"PiAction retrieved from queue: {action.cat} {action.value}")

            if action.cat == "mode":
                self.change_mode(action.value)
            elif action.cat == "obstacles":
                self.request_algo(action.value)
            elif action.cat == "snap":
                self.snap_and_rec(obstacle_id=action.value)
            elif action.cat == "single-obstacle":
                self.add_navigate_path()
            elif action.cat == "stitch":
                self.request_stitch()

    def snap_and_rec(self, obstacle_id: str) -> None:
        """
        RPi snaps an image and calls the API for image-rec.
        The response is then forwarded back to the android
        :param obstacle_id: the current obstacle ID
        """

        # notify android
        self.logger.info(f"Capturing image for obstacle id: {obstacle_id}")
        self.android_queue.put(AndroidMessage("info", f"Capturing image for obstacle id: {obstacle_id}"))

        # capture an image
        stream = io.BytesIO()
        with picamera.PiCamera() as camera:
            camera.start_preview()
            time.sleep(1)
            camera.capture(stream, format='jpeg')

        # notify android
        self.android_queue.put(AndroidMessage("info", "Image captured. Calling image-rec api..."))
        self.logger.info("Image captured. Calling image-rec api...")

        # release lock so that bot can continue moving
        self.movement_lock.release()

        # call image-rec API endpoint
        self.logger.debug("Requesting from image API")
        url = f"http://{API_IP}:{API_PORT}/image"
        filename = f"{int(time.time())}_{obstacle_id}.jpg"
        image_data = stream.getvalue()
        response = requests.post(url, files={"file": (filename, image_data)})

        if response.status_code != 200:
            self.logger.error("Something went wrong when requesting path from image-rec API. Please try again.")
            self.android_queue.put(AndroidMessage(
                "error", "Something went wrong when requesting path from image-rec API. Please try again."))
            return

        results = json.loads(response.content)

        # for stopping the robot upon finding a non-bullseye face (checklist: navigating around obstacle)
        if results.get("stop"):
            # stop issuing commands
            self.unpause.clear()

            # clear commands queue
            while not self.command_queue.empty():
                self.command_queue.get()

            self.logger.info("Found non-bullseye face, remaining commands and path cleared.")
            self.android_queue.put(AndroidMessage("info", "Found non-bullseye face, remaining commands cleared."))

        self.logger.info(f"Image recognition results: {results} ({SYMBOL_MAP.get(results['image_id'])})")

        # notify android of image-rec results
        self.android_queue.put(AndroidMessage("image-rec", results))

    def request_algo(self, data):
        """
        Requests for a series of commands and the path from the algo API
        The received commands and path are then queued in the respective queues
        If around=true, will call the /navigate endpoint instead, else /path is used
        """
        self.logger.info("Requesting path from algo...")
        self.android_queue.put(AndroidMessage("info", "Requesting path from algo..."))

        # only if outdoor mode, check if big turn is configured
        if data['mode'] == "1" and OUTDOOR_BIG_TURN:
            body = {**data, "big_turn": "1"}
        else:
            body = {**data, "big_turn": "0"}

        url = f"http://{API_IP}:{API_PORT}/path"
        response = requests.post(url, json=body)

        # error encountered at the server, return early
        if response.status_code != 200:
            # notify android
            self.android_queue.put(AndroidMessage("error", "Something went wrong when requesting path from Algo API."))
            self.logger.error("Something went wrong when requesting path from Algo API.")
            return

        # parse response
        result = json.loads(response.content)['data']
        commands = result['commands']
        path = result['path']

        # log commands received
        self.logger.debug(f"Commands received from API: {commands}")

        # replace commands from algo with outdoor commands
        if data['mode'] == '1':  # outdoor mode (from android)
            commands = list(map(self.outdoorsify, commands))
            self.logger.debug(f"Outdoorsified commands: {commands}")

        # put commands and paths into queues
        self.clear_queues()
        for c in commands:
            self.command_queue.put(c)
        for p in path[1:]:  # ignore first element as it is the starting position of the robot
            self.path_queue.put(p)

        # notify android
        self.android_queue.put(AndroidMessage("info", "Commands and path received Algo API. Robot is ready to move."))
        self.logger.info("Commands and path received Algo API. Robot is ready to move.")

    def add_navigate_path(self):
        # our hardcoded path
        hardcoded_path: List[str] = [
            "DT20", "SNAPS", "NOOP",
            "FR00", "FL00", "FW30", "BR00", "FW10", "SNAPE", "NOOP",
            "FR00", "FL00", "FW30", "BR00", "FW10", "SNAPN", "NOOP",
            "FR00", "FL00", "FW30", "BR00", "FW10", "SNAPW", "NOOP",
            "FIN"
        ]

        # put commands and paths into queues
        self.clear_queues()
        for c in hardcoded_path:
            self.command_queue.put(c)
            self.path_queue.put({
                "d": 0,
                "s": -1,
                "x": 1,
                "y": 1
            })

        self.logger.info("Navigate-around-obstacle path loaded. Robot is ready to move.")
        self.android_queue.put(AndroidMessage("info", "Navigate-around-obstacle path loaded. Robot is ready to move."))

    def request_stitch(self):
        url = f"http://{API_IP}:{API_PORT}/stitch"
        response = requests.get(url)

        # error encountered at the server, return early
        if response.status_code != 200:
            # notify android
            self.android_queue.put(AndroidMessage("error", "Something went wrong when requesting stitch from the API."))
            self.logger.error("Something went wrong when requesting stitch from the API.")
            return

        self.logger.info("Images stitched!")
        self.android_queue.put(AndroidMessage("info", "Images stitched!"))

    def change_mode(self, new_mode):
        # if robot already in correct mode
        if new_mode == "manual" and self.robot_mode.value == 0:
            self.android_queue.put(AndroidMessage('error', 'Robot already in Manual mode.'))
            self.logger.warning("Robot already in Manual mode.")
        elif new_mode == "path" and self.robot_mode.value == 1:
            self.android_queue.put(AndroidMessage('error', 'Robot already in Path mode.'))
            self.logger.warning("Robot already in Path mode.")
        else:
            # change robot mode
            self.robot_mode.value = 0 if new_mode == 'manual' else 1

            # clear command, path queues
            self.clear_queues()

            # set unpause event, so that robot can freely move
            if new_mode == "manual":
                self.unpause.set()
            else:
                self.unpause.clear()

            # release movement lock, if it was previously acquired
            try:
                self.movement_lock.release()
            except Exception:
                self.logger.warning("Tried to release a released lock!")

            # notify android
            self.android_queue.put(AndroidMessage('info', f'Robot is now in {new_mode.title()} mode.'))
            self.logger.info(f"Robot is now in {new_mode.title()} mode.")

            # buzz stm32 (1 time)
            self.stm_link.send("ZZ01")

    def clear_queues(self):
        while not self.command_queue.empty():
            self.command_queue.get()
        while not self.path_queue.empty():
            self.path_queue.get()

    def check_api(self) -> bool:
        url = f"http://{API_IP}:{API_PORT}/status"
        try:
            response = requests.get(url, timeout=1)
            if response.status_code == 200:
                self.logger.debug("API is up!")
                return True
        except ConnectionError:
            self.logger.warning("API Connection Error")
            return False
        except requests.Timeout:
            self.logger.warning("API Timeout")
            return False
        except Exception as e:
            self.logger.warning(f"API Exception: {e}")
            return False

    @staticmethod
    def outdoorsify(original):
        # for turns, only replace regular 3-1 turns (TL00), with outdoor-calibrated 3-1 turns (TL20)
        # large turns (TL30) do not need to be changed, as they are already calibrated for outdoors
        if original in ["FL00", "FR00", "BL00", "BR00"]:
            return original[:2] + "20"
        elif original.startswith("FW"):
            return original.replace("FW", "FS")
        elif original.startswith("BW"):
            return original.replace("BW", "BS")
        else:
            return original


if __name__ == "__main__":
    rpi = RaspberryPi()
    rpi.start()
