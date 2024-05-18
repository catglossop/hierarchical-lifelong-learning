import matplotlib.pyplot as plt
import os
from typing import Tuple, Sequence, Dict, Union, Optional, Callable
import numpy as np
import torch
import torch.nn as nn
from diffusers.schedulers.scheduling_ddpm import DDPMScheduler
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy

import matplotlib.pyplot as plt
import yaml
import threading
import random
import io
import base64
import requests
import tensorflow as tf

# ROS
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Image
from std_msgs.msg import Bool, Float32MultiArray
from nav_msgs.msg import Odometry
from irobot_create_msgs.msg import Dock
from rclpy.action import ActionClient
from irobot_create_msgs.action import Undock

from deployment.utils import msg_to_pil, to_numpy, transform_images, load_model
import torch
from PIL import Image as PILImage
import numpy as np
import argparse
import yaml
import time

# UTILS
from deployment.topic_names import (IMAGE_TOPIC,
                        WAYPOINT_TOPIC,
                        SAMPLED_ACTIONS_TOPIC, 
                        REACHED_GOAL_TOPIC)

# AgentLACE
from agentlace.data.data_store import QueuedDataStore
from agentlace.trainer import TrainerClient

from hierarchical_lifelong_learning.deployment.task_utils import (
    make_trainer_config,
    observation_format, 
    rlds_data_format
)


# CONSTANTS
TOPOMAP_IMAGES_DIR = "topomaps/images"
ROBOT_CONFIG_PATH ="../../deployment/config/robot.yaml"
MODEL_CONFIG_PATH = "../../deployment/config/models.yaml"
DATA_CONFIG = "../../deployment/config/data_config.yaml"
PRIMITIVES = ["Turn left", "Turn right", "Go straight", "Stop"]

class LowLevelPolicy(Node): 

    def __init__(self, 
                args
                ):
        super().__init__('low_level_policy')
        self.args = args
        self.context_queue = []
        self.context_size = None
        self.server_ip = args.ip
        self.SERVER_ADDRESS = f"http://{self.server_ip}:5001/gen_subgoal"
        self.subgoal_timeout = 10 # This is partially for debugging
        
        self.image_aspect_ratio = (4/3)
        self.starting_traj = True

        # Load the model 
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        print("Using device:", self.device)
        self.load_model_from_config(MODEL_CONFIG_PATH)

        # Load the config
        self.load_config(ROBOT_CONFIG_PATH)

        # Load data config
        self.load_data_config()

        # INFRA FOR SAVING DATA
        self.local_data_store = QueuedDataStore(capacity=100)
        self.trainer = TrainerClient(
            "lifelong_data",
            self.server_ip,
            make_trainer_config(),
            self.local_data_store,
            wait_for_server=True,
        )

        self.irobot_qos_profile = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT, 
            history=HistoryPolicy.KEEP_LAST,
            durability=DurabilityPolicy.VOLATILE,
            depth=1
            )
        
        # SUBSCRIBERS  
        self.image_msg = Image()
        self.image_sub = self.create_subscription(
            Image,
            IMAGE_TOPIC,
            self.image_callback,
            1)
        self.subgoal_image = None
        self.subgoal_sub = self.create_subscription(
            Image, 
            "/hierarchical_learning/subgoal",
            self.subgoal_callback, 
            self.irobot_qos_profile)
        self.dock_msg = Dock()
        self.dock_sub = self.create_subscription(
            Dock, 
            "/dock",
            self.dock_callback,
            self.irobot_qos_profile)
        
        # PUBLISHERS
        self.reached_goal = False
        self.reached_goal_msg = Bool()
        self.reached_goal_pub = self.create_publisher(
            Bool, 
            REACHED_GOAL_TOPIC, 
            1)
        self.reset = False 
        self.reset_sub = self.create_subscription(
            Bool, 
            "/hierarchical_learning/reset",
            1)
        self.sampled_actions_msg = Float32MultiArray()
        self.sampled_actions_pub = self.create_publisher(
            Float32MultiArray, 
            SAMPLED_ACTIONS_TOPIC, 
            1)
        self.waypoint_msg = Float32MultiArray()
        self.waypoint_pub = self.create_publisher(
            Float32MultiArray, 
            WAYPOINT_TOPIC, 
            1) 
        self.odom_msg = Odometry()
        self.current_pos = None 
        self.current_yaw = None 
        self.odom_sub = self.create_subscription(
            Odometry, 
            "/odom",
            self.odom_callback,
            1)
        
        # TIMERS
        self.timer_period = 1/self.RATE  # seconds
        self.timer = self.create_timer(self.timer_period, self.timer_callback)
        self.undock_action_client = ActionClient(self, Undock, 'undock')
    
    # Utils
    def image_to_base64(self, image):
        buffer = io.BytesIO()
        # Convert the image to RGB mode if it's in RGBA mode
        if image.mode == 'RGBA':
            image = image.convert('RGB')
        image.save(buffer, format="JPEG")
        img_str = base64.b64encode(buffer.getvalue()).decode("utf-8")
        return img_str
    
    def unnormalize_data(self, ndata, stats):
        ndata = (ndata + 1) / 2
        data = ndata * (stats['max'] - stats['min']) + stats['min']
        return data

    def get_delta(self, actions):
        # append zeros to first action
        ex_actions = np.concatenate([np.zeros((actions.shape[0],1,actions.shape[-1])), actions], axis=1)
        delta = ex_actions[:,1:] - ex_actions[:,:-1]
        return delta

    def get_action(self):
        # diffusion_output: (B, 2*T+1, 1)
        # return: (B, T-1)
        ndeltas = self.naction
        ndeltas = ndeltas.reshape(ndeltas.shape[0], -1, 2)
        ndeltas = to_numpy(ndeltas)
        ndeltas = self.unnormalize_data(ndeltas, self.ACTION_STATS)
        actions = np.cumsum(ndeltas, axis=1)
        return torch.from_numpy(actions).to(self.device)

    def transform_image_to_string(self, pil_img, image_size, center_crop=False):
        """Transforms a PIL image to a torch tensor."""
        w, h = pil_img.size
        if center_crop: 
            if w > h:
                pil_img = TF.center_crop(pil_img, (h, int(h * self.image_aspect_ratio)))
            else:
                pil_img = TF.center_crop(pil_img, (int(w / self.image_aspect_ratio), w))
        pil_img = pil_img.resize(image_size)
        image_bytes_io = io.BytesIO()
        PILImage.fromarray(np.array(self.obs)).save(image_bytes_io, format = 'JPEG')
        image_bytes = tf.constant(image_bytes_io.getvalue(), dtype = tf.string)
        return image_bytes

    # Loading 
    def load_config(self, robot_config_path):
        with open(robot_config_path, "r") as f:
            robot_config = yaml.safe_load(f)
        self.MAX_V = robot_config["max_v"]
        self.MAX_W = robot_config["max_w"]
        self.VEL_TOPIC = "/task_vel"
        self.DT = 1/robot_config["frame_rate"]
        self.RATE = robot_config["frame_rate"]
        self.EPS = 1e-8
    
    def load_model_from_config(self, model_paths_config):
        # Load configs
        with open(model_paths_config, "r") as f:
            model_paths = yaml.safe_load(f)

        model_config_path = model_paths["nomad"]["config_path"]
        with open(model_config_path, "r") as f:
            self.model_params = yaml.safe_load(f)

        self.context_size = self.model_params["context_size"] 

        # Load model weights
        self.ckpth_path = model_paths["nomad"]["ckpt_path"]
        if os.path.exists(self.ckpth_path):
            print(f"Loading model from {self.ckpth_path}")
        else:
            raise FileNotFoundError(f"Model weights not found at {self.ckpth_path}")
        self.model = load_model(
            self.ckpth_path,
            self.model_params,
            self.device,
        )
        self.model = self.model.to(self.device)
        self.model.eval()

        self.num_diffusion_iters = self.model_params["num_diffusion_iters"]
        self.noise_scheduler = DDPMScheduler(
            num_train_timesteps=self.model_params["num_diffusion_iters"],
            beta_schedule='squaredcos_cap_v2',
            clip_sample=True,
            prediction_type='epsilon'
        )
        
    def load_data_config(self):
        # LOAD DATA CONFIG
        with open(os.path.join(os.path.dirname(__file__), DATA_CONFIG), "r") as f:
            data_config = yaml.safe_load(f)
        # POPULATE ACTION STATS
        self.ACTION_STATS = {}
        for key in data_config['action_stats']:
            self.ACTION_STATS[key] = np.array(data_config['action_stats'][key])

    # TODO: add subscription to VLM planner to get the goal
    def send_image_to_server(self, image: PILImage.Image) -> dict:
        image_base64 = self.image_to_base64(image)
        prompt = random.choice(PRIMITIVES)
        data = {
            'curr': image_base64,
            'hl_prompt': prompt,
            'll_prompt': prompt,
        }
        response = requests.post(self.SERVER_ADDRESS, json=data, timeout=99999999)
        data = response.json()
        img_data = base64.b64decode(data['goal'])
        subgoal = PILImage.open(io.BytesIO(img_data))
        subgoal = np.array(subgoal)
        return subgoal

    def send_undock(self):
        goal_msg = Undock.Goal()
        
        self.undock_action_client.wait_for_server()

        return self.undock_action_client.send_goal_async(goal_msg)
    
    def dock_callback(self, msg):
        self.is_docked = msg.is_docked

    def state_callback(self, msg):
        self.state = msg.data

    def odom_callback(self, odom_msg: Odometry):
        """Callback function for the odometry subscriber"""
        self.current_yaw = odom_msg.pose.pose.orientation.z
        self.current_pos = np.array([odom_msg.pose.pose.position.x, odom_msg.pose.pose.position.y])
        
    def image_callback(self, msg):
        self.image_msg = msg_to_pil(msg)
        self.obs_bytes = self.transform_image_to_string(self.image_msg)
        if self.context_size is not None:
            if len(self.context_queue) < self.context_size + 1:
                self.context_queue.append(self.image_msg)
            else:
                self.context_queue.pop(0)
                self.context_queue.append(self.image_msg)
    
    def subgoal_callback(self, msg):
        print("Subgoal received")
        self.subgoal_image = msg_to_pil(msg)
        self.act_status = False
    
    def process_images(self):
        self.obs_images = transform_images(self.context_queue, self.model_params["image_size"], center_crop=True)
        self.obs_images = torch.split(self.obs_images, 3, dim=1)
        self.obs_images = torch.cat(self.obs_images, dim=1) 
        self.obs_images = self.obs_images.to(self.device)
        self.mask = torch.zeros(1).long().to(self.device)  
    
    def infer_actions(self):
        # Get early fusion obs goal for conditioning
        self.obsgoal_cond = self.model('vision_encoder', 
                                        obs_img=self.obs_images.repeat(len(self.goal_image), 1, 1, 1), 
                                        goal_img=self.goal_image, 
                                        input_goal_mask=self.mask.repeat(len(self.goal_image)))

        self.obs_cond = self.obsgoal_cond[0].unsqueeze(0)

        self.dists = self.model("dist_pred_net", obsgoal_cond=self.obsgoal_cond)
        self.dists = to_numpy(self.dists.flatten())
        print("DISTANCE TO SUBGOAL: ", self.dists[0])

        # infer action
        with torch.no_grad():
            # encoder vision features
            if len(self.obs_cond.shape) == 2:
                self.obs_cond = self.obs_cond.repeat(self.args.num_samples, 1)
            else:
                self.obs_cond = self.obs_cond.repeat(self.args.num_samples, 1, 1)
            
            # initialize action from Gaussian noise
            self.noisy_action = torch.randn(
                (self.args.num_samples, self.model_params["len_traj_pred"], 2), device=self.device)
            self.naction = self.noisy_action

            # init scheduler
            self.noise_scheduler.set_timesteps(self.num_diffusion_iters)

            self.start_time = time.time()
            for k in self.noise_scheduler.timesteps[:]:
                # predict noise
                self.noise_pred = self.model(
                    'noise_pred_net',
                    sample=self.naction,
                    timestep=k,
                    global_cond=self.obs_cond
                )
                # inverse diffusion step (remove noise)
                self.naction = self.noise_scheduler.step(
                    model_output=self.noise_pred,
                    timestep=k,
                    sample=self.naction
                ).prev_sample
            print("time elapsed:", time.time() - self.start_time)
            print("DEVICE: " , self.device)

        self.naction = to_numpy(self.get_action())
        self.sampled_actions_msg = Float32MultiArray()
        self.sampled_actions_msg.data = np.concatenate((np.array([0]), self.naction.flatten())).tolist()
        print("published sampled actions")
        self.sampled_actions_pub.publish(self.sampled_actions_msg)
        self.naction = self.naction[0] 
        self.chosen_waypoint = self.naction[self.args.waypoint] 

    def timer_callback(self):

        self.chosen_waypoint = np.zeros(4, dtype=np.float32)
        if len(self.context_queue) > self.model_params["context_size"] and not self.is_docked:

            if not self.wait_for_reset: 
                # Process observations
                self.process_images()

                # Check if goal reached
                if self.dists[0] < 10: 
                    self.reached_goal = True
                
                # Get goal image
                self.goal_image = transform_images(self.subgoal_image, self.model_params["image_size"], center_crop=False).to(self.device)

                # Use policy to get actions
                self.infer_actions()
                if DEBUG: 
                    print("Traj dur: ", self.traj_duration)
                    print("Current state: ", self.state)
                    print("Goal reached: ", self.reached_goal)
                    print("Wait for reset: ", self.wait_for_reset)
                if self.traj_duration > self.subgoal_timeout:
                    self.traj_duration = 0
                    self.is_terminal = True 
                    self.status = "timeout"
                elif self.state == "reset":
                    self.traj_duration = 0
                    self.is_terminal = True 
                    self.status = "crash"
                    self.wait_for_reset = True 
                elif self.reached_goal:
                    self.traj_duration = 0
                    self.is_terminal = True 
                    self.status = "reached_goal"
                elif self.state in ["idle", "nav_to_dock", "dock", "undock", "teleop"]:
                    self.traj_duration = 0
                    self.is_terminal = True
                    self.status = "manual"
                    self.wait_for_reset = True
                else:
                    self.is_terminal = False
                    self.status = "running"

                self.curr_obs = {
                    "obs" : self.obs_bytes, 
                    "position" : self.current_pos,
                    "yaw": self.current_yaw, 
                }
                formatted_obs = {
                    "observation": self.curr_obs,
                    "goal": self.goal_bytes
                    "is_first": self.starting_traj,
                    "is_last": is_terminal,
                    "is_terminal": is_terminal,
                    "status": self.status
                }
                if self.starting_traj: 
                    self.starting_traj = False 
                self.data_store.insert(formatted_obs)
                self.traj_duration += 1

            if self.reached_goal or self.traj_duration > self.subgoal_timeout or (self.state == "do_task" and self.wait_for_reset): 
                # Update the goal image
                self.subgoal_image = self.send_image_to_server(self.image_msg)
                self.subgoal_image = np.array(self.subgoal).astype(np.uint8)
                self.goal_bytes = self.transform_image_to_string(PILImage.fromarray(self.subgoal_image))
                self.starting_traj = True
                self.reached_goal = False
                self.wait_for_reset = False

        # Normalize and publish waypoint
        if self.model_params["normalize"]:
            self.chosen_waypoint[:2] *= (self.MAX_V / self.RATE)  

        self.waypoint_msg.data = self.chosen_waypoint.tolist()
        self.waypoint_pub.publish(self.waypoint_msg)




def main(args):
    rclpy.init()
    low_level_policy = LowLevelPolicy(args)
    while low_level_policy.is_docked:
        low_level_policy.send_undock()

    rclpy.spin(low_level_policy)
    low_level_policy.destroy_node()
    rclpy.shutdown()
    
    print("Registered with master node. Waiting for image observations...")
    print(f"Using {low_level_policy.device}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Code to run GNM DIFFUSION EXPLORATION on the locobot")
    parser.add_argument(
        "--model",
        "-m",
        default="nomad",
        type=str,
        help="model name (only nomad is supported) (hint: check ../config/models.yaml) (default: nomad)",
    )
    parser.add_argument(
        "--waypoint",
        "-w",
        default=2, # close waypoints exihibit straight line motion (the middle waypoint is a good default)
        type=int,
        help=f"""index of the waypoint used for navigation (between 0 and 4 or 
        how many waypoints your model predicts) (default: 2)""",
    )
    parser.add_argument(
        "--dir",
        "-d",
        default="topomap",
        type=str,
        help="path to topomap images",
    )
    parser.add_argument(
        "--goal-node",
        "-g",
        default=-1,
        type=int,
        help="""goal node index in the topomap (if -1, then the goal node is 
        the last node in the topomap) (default: -1)""",
    )
    parser.add_argument(
        "--close-threshold",
        "-t",
        default=3,
        type=int,
        help="""temporal distance within the next node in the topomap before 
        localizing to it (default: 3)""",
    )
    parser.add_argument(
        "--radius",
        "-r",
        default=4,
        type=int,
        help="""temporal number of locobal nodes to look at in the topopmap for
        localization (default: 2)""",
    )
    parser.add_argument(
        "--num-samples",
        "-n",
        default=8,
        type=int,
        help=f"Number of actions sampled from the exploration model (default: 8)",
    )
    parser.add_argument(
        "--ip", 
        "-i", 
        default="localhost",
        type=str,
    )
    args = parser.parse_args()
    main(args)


