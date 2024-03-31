#!/usr/bin/env python3
import json
import time

import requests
from multiprocessing import Process, Manager
from stm32 import STMLink
from logger import prepare_logger
from settings import API_IP, API_PORT

data = [
            "WN21"
        ]
        

class PiAction:
    """
    Class that represents an action that the RPi needs to take.    
    """

    def __init__(self, cat, value):
        """
        :param cat: The category of the action. Can be 'info', 'mode', 'path', 'snap', 'obstacle', 'location', 'failed', 'success'
        :param value: The value of the action. Can be a string, a list of coordinates, or a list of obstacles.
        """
        self._cat = cat
        self._value = value

    @property
    def cat(self):
        return self._cat

    @property
    def value(self):
        return self._value
class SimplifiedRaspberryPi:
    def __init__(self):
        # Initialize logger and STM communication
        self.logger = prepare_logger()
        self.stm_link = STMLink()
        self.manager = Manager()

        #self.android_dropped = self.manager.Event()
        self.unpause = self.manager.Event()

        self.movement_lock = self.manager.Lock()

        #self.android_queue = self.manager.Queue()  # Messages to send to Android
        # Messages that need to be processed by RPi
        self.rpi_action_queue = self.manager.Queue()
        # Messages that need to be processed by STM32, as well as snap commands
        self.command_queue = self.manager.Queue()
        # X,Y,D coordinates of the robot after execution of a command
        self.path_queue = self.manager.Queue()

        #self.proc_recv_android = None
        self.proc_recv_stm32 = None
        #self.proc_android_sender = None
        self.proc_command_follower = None
        self.proc_rpi_action = None
        self.rs_flag = False
        self.success_obstacles = self.manager.list()
        self.failed_obstacles = self.manager.list()
        self.obstacles = self.manager.dict()
        self.current_location = self.manager.dict()
        self.failed_attempt = False


    def start(self):
        """Starts the simplified RPi orchestrator."""
        try:
            # Establish connection with STM32
            self.stm_link.connect()

            # Define child processes
            #self.proc_recv_android = Process(target=self.recv_android)
            self.proc_recv_stm32 = Process(target=self.recv_stm)
            #self.proc_command_follower = Process(target=self.command_follower)
            #self.proc_rpi_action = Process(target=self.rpi_action)
            # Start child processes
            self.proc_recv_stm32.start()
            #self.proc_command_follower.start()
            #self.proc_rpi_action.start()
            # Make POST request to the algorithm API
            # Expected command output:
            #"commands": ["FW90","FW70","FR00","FW90","FW10","SNAP1_C","FIN"]
            response = 1 #self.post_request_to_algorithm_api()
            #(response)

            if response:
                commands =  data
                self.send_commands_to_stm(commands)
                #print(commands)
            else:
                self.logger.error("Failed to get response from the algorithm API.")

        except KeyboardInterrupt:
            self.logger.info("Program exited!")
        finally:
            self.stm_link.disconnect()

    def post_request_to_algorithm_api(self):
        """Sends POST request to the algorithm API with hardcoded input."""
        url = f"http://{API_IP}:{API_PORT}/path"  # Assuming the endpoint for posting is '/path'
        #url = f"http://{API_IP}:{API_PORT}"
        data = {
            "obstacles": [
                {
                    "x": 18,
                    "y": 18,
                    "d": 6,
                    "id": "1"
                }
            ],
            "retrying": False,
            "robot_x": 1,
            "robot_y": 1,
            "robot_dir": 0
        }
        self.logger.info(f"Sending POST request to {url} with data: {data}")
        try:
            response = requests.post(url, json=data)
            if response.status_code == 200:
                return response.json()
            else:
                self.logger.error(f"Failed to get a valid response: {response.status_code}")
        except Exception as e:
            self.logger.error(f"Exception occurred during API request: {e}")
        return None

    def parse_response(self, response):
        """Parses the API response to extract movement commands."""
        # Example of parsing, adjust based on the actual API response format
        # Just return the commands 
        commands = response["data"].get("commands", [])
        return commands

    def send_commands_to_stm(self, commands):
        """Sends commands to the STM32 to move the robot."""
        #time.sleep(6)
        for command in commands:
            self.logger.info(f"Sending command to STM: {command}")
            ret = self.stm_link.send(command)
            #self.movement_lock.acquire()
            print(f"return value {ret}")
            #time.sleep(1)  # Adjust delay as necessary
            
    def recv_stm(self) -> None:
        """
        [Child Process] Receive acknowledgement messages from STM32, and release the movement lock
        """
        while True:

            message: str = self.stm_link.recv()
        
            if message.startswith("ACK"):
                if self.rs_flag == False:
                    self.rs_flag = True
                    self.logger.debug("ACK for RS00 from STM32 received.")
                    continue
                try:
                    self.movement_lock.release()
                    try:
                        self.retrylock.release()
                    except:
                        pass
                    self.logger.debug(
                        "ACK from STM32 received, movement lock released.")

                    cur_location = self.path_queue.get_nowait()

                    self.current_location['x'] = cur_location['x']
                    self.current_location['y'] = cur_location['y']
                    self.current_location['d'] = cur_location['d']
                    self.logger.info(
                        f"self.current_location = {self.current_location}")
                    self.android_queue.put(AndroidMessage('location', {
                        "x": cur_location['x'],
                        "y": cur_location['y'],
                        "d": cur_location['d'],
                    }))

                except Exception:
                    self.logger.warning("Tried to release a released lock!")
            else:
                self.logger.warning(
                    f"Ignored unknown message from STM: {message}")
                
    def command_follower(self) -> None:
        """
        [Child Process] 
        """
        while True:
            # Retrieve next movement command
            command: str = self.command_queue.get()
            self.logger.debug("wait for unpause")
            # Wait for unpause event to be true [Main Trigger]
            try:
                self.logger.debug("wait for retrylock")
                self.retrylock.acquire()
                self.retrylock.release()
            except:
                self.logger.debug("wait for unpause")
                self.unpause.wait()
            self.logger.debug("wait for movelock")
            # Acquire lock first (needed for both moving, and snapping pictures)
            self.movement_lock.acquire()

            # STM32 Commands - Send straight to STM32
            stm32_prefixes = ("FS", "BS", "FW", "BW", "FL", "FR", "BL",
                              "BR", "TL", "TR", "A", "C", "DT", "STOP", "ZZ", "RS")
            if command.startswith(stm32_prefixes):
                self.stm_link.send(command)
                self.logger.debug(f"Sending to STM32: {command}")

            # Snap command
            elif command.startswith("SNAP"):
                obstacle_id_with_signal = command.replace("SNAP", "")

                self.rpi_action_queue.put(
                    PiAction(cat="snap", value=obstacle_id_with_signal))

            # End of path
            elif command == "FIN":
                self.logger.info(
                    f"At FIN, self.failed_obstacles: {self.failed_obstacles}")
                self.logger.info(
                    f"At FIN, self.current_location: {self.current_location}")
                if len(self.failed_obstacles) != 0 and self.failed_attempt == False:

                    new_obstacle_list = list(self.failed_obstacles)
                    for i in list(self.success_obstacles):
                        # {'x': 5, 'y': 11, 'id': 1, 'd': 4}
                        i['d'] = 8
                        new_obstacle_list.append(i)

                    self.logger.info("Attempting to go to failed obstacles")
                    self.failed_attempt = True
                    self.request_algo({'obstacles': new_obstacle_list, 'mode': '0'},
                                      self.current_location['x'], self.current_location['y'], self.current_location['d'], retrying=True)
                    self.retrylock = self.manager.Lock()
                    self.movement_lock.release()
                    continue

                self.unpause.clear()
                self.movement_lock.release()
                self.logger.info("Commands queue finished.")
                self.android_queue.put(AndroidMessage(
                    "info", "Commands queue finished."))
                self.android_queue.put(AndroidMessage("status", "finished"))
                self.rpi_action_queue.put(PiAction(cat="stitch", value=""))
            else:
                raise Exception(f"Unknown command: {command}")
    def rpi_action(self):
        """
        [Child Process] 
        """
        while True:
            action: PiAction = self.rpi_action_queue.get()
            self.logger.debug(
                f"PiAction retrieved from queue: {action.cat} {action.value}")

            if action.cat == "obstacles":
                for obs in action.value['obstacles']:
                    self.obstacles[obs['id']] = obs
                self.request_algo(action.value)
            elif action.cat == "snap":
                self.snap_and_rec(obstacle_id_with_signal=action.value)
            elif action.cat == "stitch":
                self.request_stitch()

    def snap_and_rec(self, obstacle_id_with_signal: str) -> None:
        """
        RPi snaps an image and calls the API for image-rec.
        The response is then forwarded back to the android
        :param obstacle_id_with_signal: the current obstacle ID followed by underscore followed by signal
        """
        obstacle_id, signal = obstacle_id_with_signal.split("_")
        self.logger.info(f"Capturing image for obstacle id: {obstacle_id}")
        self.android_queue.put(AndroidMessage(
            "info", f"Capturing image for obstacle id: {obstacle_id}"))
        url = f"http://{API_IP}:{API_PORT}/image"
        filename = f"{int(time.time())}_{obstacle_id}_{signal}.jpg"

        con_file = "PiLCConfig9.txt"
        Home_Files = []
        Home_Files.append(os.getlogin())
        config_file = "/home/" + Home_Files[0] + "/" + con_file

        extns = ['jpg', 'png', 'bmp', 'rgb', 'yuv420', 'raw']
        shutters = [-2000, -1600, -1250, -1000, -800, -640, -500, -400, -320, -288, -250, -240, -200, -160, -144, -125, -120, -100, -96, -80, -60, -50, -48, -40, -30, -25, -20, -
                    15, -13, -10, -8, -6, -5, -4, -3, 0.4, 0.5, 0.6, 0.8, 1, 1.1, 1.2, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 15, 20, 25, 30, 40, 50, 60, 75, 100, 112, 120, 150, 200, 220, 230, 239, 435]
        meters = ['centre', 'spot', 'average']
        awbs = ['off', 'auto', 'incandescent', 'tungsten',
                'fluorescent', 'indoor', 'daylight', 'cloudy']
        denoises = ['off', 'cdn_off', 'cdn_fast', 'cdn_hq']

        config = []
        with open(config_file, "r") as file:
            line = file.readline()
            while line:
                config.append(line.strip())
                line = file.readline()
            config = list(map(int, config))
        mode = config[0]
        speed = config[1]
        gain = config[2]
        brightness = config[3]
        contrast = config[4]
        red = config[6]
        blue = config[7]
        ev = config[8]
        extn = config[15]
        saturation = config[19]
        meter = config[20]
        awb = config[21]
        sharpness = config[22]
        denoise = config[23]
        quality = config[24]

        retry_count = 0

        while True:

            retry_count += 1

            shutter = shutters[speed]
            if shutter < 0:
                shutter = abs(1/shutter)
            sspeed = int(shutter * 1000000)
            if (shutter * 1000000) - int(shutter * 1000000) > 0.5:
                sspeed += 1

            rpistr = "libcamera-still -e " + \
                extns[extn] + " -n -t 500 -o " + filename
            rpistr += " --brightness " + \
                str(brightness/100) + " --contrast " + str(contrast/100)
            rpistr += " --shutter " + str(sspeed)
            if ev != 0:
                rpistr += " --ev " + str(ev)
            if sspeed > 1000000 and mode == 0:
                rpistr += " --gain " + str(gain) + " --immediate "
            else:
                rpistr += " --gain " + str(gain)
                if awb == 0:
                    rpistr += " --awbgains " + str(red/10) + "," + str(blue/10)
                else:
                    rpistr += " --awb " + awbs[awb]
            rpistr += " --metering " + meters[meter]
            rpistr += " --saturation " + str(saturation/10)
            rpistr += " --sharpness " + str(sharpness/10)
            rpistr += " --quality " + str(quality)
            rpistr += " --denoise " + denoises[denoise]
            rpistr += " --metadata - --metadata-format txt >> PiLibtext.txt"

            os.system(rpistr)

            self.logger.debug("Requesting from image API")

            response = requests.post(
                url, files={"file": (filename, open(filename, 'rb'))})

            if response.status_code != 200:
                self.logger.error(
                    "Something went wrong when requesting path from image-rec API. Please try again.")
                return

            results = json.loads(response.content)

            # Higher brightness retry

            if results['image_id'] != 'NA' or retry_count > 6:
                break
            elif retry_count > 3:
                self.logger.info(f"Image recognition results: {results}")
                self.logger.info("Recapturing with lower shutter speed...")
                speed -= 1
            elif retry_count <= 3:
                self.logger.info(f"Image recognition results: {results}")
                self.logger.info("Recapturing with higher shutter speed...")
                speed += 1

        # release lock so that bot can continue moving
        self.movement_lock.release()
        try:
            self.retrylock.release()
        except:
            pass

        self.logger.info(f"results: {results}")
        self.logger.info(f"self.obstacles: {self.obstacles}")
        self.logger.info(
            f"Image recognition results: {results} ({SYMBOL_MAP.get(results['image_id'])})")

        if results['image_id'] == 'NA':
            self.failed_obstacles.append(
                self.obstacles[int(results['obstacle_id'])])
            self.logger.info(
                f"Added Obstacle {results['obstacle_id']} to failed obstacles.")
            self.logger.info(f"self.failed_obstacles: {self.failed_obstacles}")
        else:
            self.success_obstacles.append(
                self.obstacles[int(results['obstacle_id'])])
            self.logger.info(
                f"self.success_obstacles: {self.success_obstacles}")
        self.android_queue.put(AndroidMessage("image-rec", results))

if __name__ == "__main__":
    rpi = SimplifiedRaspberryPi()
    rpi.start()
