# Copyright 1996-2020 Cyberbotics Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ROS2 e-puck driver."""

from functools import partial
from math import pi, cos, sin
import rclpy
from tf2_ros import TransformBroadcaster
from sensor_msgs.msg import Range, Image, CameraInfo, Imu, LaserScan
from geometry_msgs.msg import Twist, Quaternion, TransformStamped
from nav_msgs.msg import Odometry
from webots_ros2_msgs.srv import SetInt
from webots_ros2_core.webots_node import WebotsNode
import time
from rcl_interfaces.msg import SetParametersResult
from builtin_interfaces.msg import Time


PERIOD_MS = 64
PERIOD_S = PERIOD_MS / 1000.0
OUT_OF_RANGE = 0.0
ENCODER_RESOLUTION = 1000.0
TOF_MAX_RANGE = 0.0
TOF_MIN_RANGE = 1.0
INFRARED_MAX_RANGE = 0.04
INFRARED_MIN_RANGE = 0.009
GROUND_MIN_RANGE = 0.0
GROUND_MAX_RANGE = 0.016
DEFAULT_WHEEL_RADIUS = 0.02
DEFAULT_WHEEL_DISTANCE = 0.05685
NB_LIGHT_SENSORS = 8
NB_GROUND_SENSORS = 3
NB_RGB_LEDS = 4
NB_BINARY_LEDS = 4
NB_INFRARED_SENSORS = 8
SENSOR_DIST_FROM_CENTER = 0.035

TOF_TABLE = [
    [2.00, 2000.0],
    [1.70, 1780.5],
    [1.00, 1052.0],
    [0.50, 531.9],
    [0.20, 218.9],
    [0.10, 111.0],
    [0.05, 58.5],
    [0.00, 19.8]
]

DISTANCE_TABLE = [
    [0, 4095],
    [0.005, 2133.33],
    [0.01, 1465.73],
    [0.015, 601.46],
    [0.02, 383.84],
    [0.03, 234.93],
    [0.04, 158.03],
    [0.05, 120],
    [0.06, 104.09]
]


def euler_to_quaternion(roll, pitch, yaw):
    """Source: https://computergraphics.stackexchange.com/a/8229."""
    q = Quaternion()
    q.x = sin(roll/2) * cos(pitch/2) * cos(yaw/2) - \
        cos(roll/2) * sin(pitch/2) * sin(yaw/2)
    q.y = cos(roll/2) * sin(pitch/2) * cos(yaw/2) + \
        sin(roll/2) * cos(pitch/2) * sin(yaw/2)
    q.z = cos(roll/2) * cos(pitch/2) * sin(yaw/2) - \
        sin(roll/2) * sin(pitch/2) * cos(yaw/2)
    q.w = cos(roll/2) * cos(pitch/2) * cos(yaw/2) + \
        sin(roll/2) * sin(pitch/2) * sin(yaw/2)
    return q


def interpolate_function(value, startX, startY, endX, endY):
    slope = (endY - startY) / (endX - startX)
    return slope * (value - startX) + startY


def interpolate_table(value, table):
    for i in range(len(table) - 1):
        if (value < table[i][1] and value >= table[i + 1][1]) or \
                (value > table[i][1] and value <= table[i + 1][1]):
            return interpolate_function(
                value,
                table[i][1],
                table[i][0],
                table[i + 1][1],
                table[i + 1][0]
            )
    # Edge case, search outside of two points.
    # This code assumes that the table is sorted in descending order
    if value > table[0][1]:
        # Interpolate as first
        return interpolate_function(
            value,
            table[0][1],
            table[0][0],
            table[1][1],
            table[1][0]
        )
    else:
        # Interpolate as last
        return interpolate_function(
            value,
            table[len(table) - 2][1],
            table[len(table) - 2][0],
            table[len(table) - 1][1],
            table[len(table) - 1][0]
        )


class EPuckDriver(WebotsNode):
    def __init__(self, args):
        super().__init__('epuck_driver', args)

        # Parameters
        wheel_distance_param = self.declare_parameter("wheel_distance", 0.0552)
        wheel_radius_param = self.declare_parameter("wheel_radius", 0.021)
        camera_period_param = self.declare_parameter(
            "camera_period", self.timestep)
        self.period = self.declare_parameter("period", self.timestep)
        self.camera_period = camera_period_param.value
        self.wheel_radius = wheel_radius_param.value
        self.wheel_distance = wheel_distance_param.value
        self.set_parameters_callback(self.on_param_changed)

        # Initialize motors
        self.left_motor = self.robot.getMotor('left wheel motor')
        self.right_motor = self.robot.getMotor('right wheel motor')
        self.left_motor.setPosition(float('inf'))
        self.right_motor.setPosition(float('inf'))
        self.left_motor.setVelocity(0)
        self.right_motor.setVelocity(0)
        self.create_subscription(Twist, '/cmd_vel', self.cmd_vel_callback, 1)
        self.get_logger().info('EPuck Initialized')

        # Initialize odometry
        self.reset_odometry()
        self.left_wheel_sensor = self.robot.getPositionSensor(
            'left wheel sensor')
        self.right_wheel_sensor = self.robot.getPositionSensor(
            'right wheel sensor')
        self.left_wheel_sensor.enable(self.period.value)
        self.right_wheel_sensor.enable(self.period.value)
        self.odometry_publisher = self.create_publisher(Odometry, '/odom', 1)

        # Initialize IMU
        self.gyro = self.robot.getGyro('gyro')
        self.gyro.enable(self.period.value)
        self.accelerometer = self.robot.getAccelerometer('accelerometer')
        self.accelerometer.enable(self.period.value)
        self.imu_publisher = self.create_publisher(Imu, '/imu', 10)

        # Intialize distance sensors
        self.sensor_publishers = {}
        self.sensors = {}
        for i in range(8):
            sensor = self.robot.getDistanceSensor('ps{}'.format(i))
            sensor.enable(self.period.value)
            sensor_publisher = self.create_publisher(
                Range, '/ps{}'.format(i), 10)
            self.sensors['ps{}'.format(i)] = sensor
            self.sensor_publishers['ps{}'.format(i)] = sensor_publisher

        self.tof_sensor = self.robot.getDistanceSensor('tof')
        self.tof_sensor.enable(self.period.value)
        self.tof_publisher = self.create_publisher(Range, '/tof', 1)
        self.laser_publisher = self.create_publisher(LaserScan, '/scan', 1)

        # Initialize camera
        self.camera = self.robot.getCamera('camera')
        self.camera.enable(self.camera_period)
        self.camera_publisher = self.create_publisher(
            Image, '/image_raw', 10)
        # self.create_timer(self.camera_period / 1000, self.camera_callback)
        self.camera_info_publisher = self.create_publisher(
            CameraInfo, '/image_raw/camera_info', 10)

        # Initialize LEDs
        self.leds = []
        self.led_services = []
        for i in range(8):
            led = self.robot.getLED('led{}'.format(i))
            led_service = self.create_service(
                SetInt, '/set_led{}'.format(i), partial(self.led_callback, index=i))
            self.leds.append(led)
            self.led_services.append(led_service)

        # Main loop
        self.create_timer(self.period.value / 1000, self.step_callback)

        # Transforms
        self.tf_broadcaster = TransformBroadcaster(self)

    def reset_odometry(self):
        self.prev_left_wheel_ticks = 0
        self.prev_right_wheel_ticks = 0
        self.prev_position = (0.0, 0.0)
        self.prev_angle = 0.0

    def on_param_changed(self, params):
        result = SetParametersResult()
        result.successful = True

        for param in params:
            if param.name == "wheel_radius":
                self.reset_odometry()
                self.wheel_radius = param.value
            elif param.name == "wheel_distance":
                self.reset_odometry()
                self.wheel_distance = param.value

        return result

    def step_callback(self):
        self.robot.step(self.period.value)

        epoch = time.time()
        stamp = Time()
        stamp.sec = int(epoch)
        stamp.nanosec = int((epoch - int(epoch)) * 1E9)

        self.odometry_callback(stamp)
        self.distance_callback(stamp)

    def cmd_vel_callback(self, twist):
        self.get_logger().info('Message received')
        left_velocity = (2.0 * twist.linear.x - twist.angular.z *
                         self.wheel_distance) / (2.0 * self.wheel_radius)
        right_velocity = (2.0 * twist.linear.x + twist.angular.z *
                          self.wheel_distance) / (2.0 * self.wheel_radius)
        self.left_motor.setVelocity(left_velocity)
        self.right_motor.setVelocity(right_velocity)

    def odometry_callback(self, stamp):
        encoder_period_s = self.period.value / 1000.0
        left_wheel_ticks = self.left_wheel_sensor.getValue()
        right_wheel_ticks = self.right_wheel_sensor.getValue()

        # Calculate velocities
        v_left_rad = (left_wheel_ticks -
                      self.prev_left_wheel_ticks) / encoder_period_s
        v_right_rad = (right_wheel_ticks -
                       self.prev_right_wheel_ticks) / encoder_period_s
        v_left = v_left_rad * self.wheel_radius
        v_right = v_right_rad * self.wheel_radius
        v = (v_left + v_right) / 2
        omega = (v_right - v_left) / self.wheel_distance

        # Calculate position & angle
        # Fourth order Runge - Kutta
        # Reference: https://www.cs.cmu.edu/~16311/s07/labs/NXTLabs/Lab%203.html
        k00 = v * cos(self.prev_angle)
        k01 = v * sin(self.prev_angle)
        k02 = omega
        k10 = v * cos(self.prev_angle + encoder_period_s * k02 / 2)
        k11 = v * sin(self.prev_angle + encoder_period_s * k02 / 2)
        k12 = omega
        k20 = v * cos(self.prev_angle + encoder_period_s * k12 / 2)
        k21 = v * sin(self.prev_angle + encoder_period_s * k12 / 2)
        k22 = omega
        k30 = v * cos(self.prev_angle + encoder_period_s * k22 / 2)
        k31 = v * sin(self.prev_angle + encoder_period_s * k22 / 2)
        k32 = omega
        position = [
            self.prev_position[0] + (encoder_period_s / 6) *
            (k00 + 2 * (k10 + k20) + k30),
            self.prev_position[1] + (encoder_period_s / 6) *
            (k01 + 2 * (k11 + k21) + k31)
        ]
        angle = self.prev_angle + \
            (encoder_period_s / 6) * (k02 + 2 * (k12 + k22) + k32)

        # Update variables
        self.prev_position = position.copy()
        self.prev_angle = angle
        self.prev_left_wheel_ticks = left_wheel_ticks
        self.prev_right_wheel_ticks = right_wheel_ticks

        # Pack & publish odometry
        msg = Odometry()
        msg.header.stamp = stamp
        msg.header.frame_id = 'odom'
        msg.child_frame_id = 'base_link'
        msg.twist.twist.linear.x = v
        msg.twist.twist.angular.z = omega
        msg.pose.pose.position.x = position[0]
        msg.pose.pose.position.y = position[1]
        msg.pose.pose.orientation = euler_to_quaternion(0, 0, angle)
        self.odometry_publisher.publish(msg)

        # Pack & publish transforms
        tf = TransformStamped()
        tf.header.stamp = stamp
        tf.header.frame_id = 'odom'
        tf.child_frame_id = 'base_link'
        tf.transform.translation.x = position[0]
        tf.transform.translation.y = position[1]
        tf.transform.translation.z = 0.0
        tf.transform.rotation = euler_to_quaternion(0, 0, angle)
        self.tf_broadcaster.sendTransform(tf)

    def distance_callback(self, stamp):
        dists = [OUT_OF_RANGE] * NB_INFRARED_SENSORS
        dist_tof = OUT_OF_RANGE

        # Calculate distances
        for i, key in enumerate(self.sensors):
            dists[i] = interpolate_table(
                self.sensors[key].getValue(), DISTANCE_TABLE)
        dist_tof = interpolate_table(self.sensors[key].getValue(), TOF_TABLE)

        # Publish range
        for i, key in enumerate(self.sensors):
            msg = Range()
            msg.field_of_view = self.sensors[key].getAperture()
            msg.min_range = INFRARED_MIN_RANGE
            msg.max_range = INFRARED_MAX_RANGE
            msg.range = dists[i]
            msg.radiation_type = Range.INFRARED
            self.sensor_publishers[key].publish(msg)

        # Max range of ToF sensor is 2m so we put it as maximum laser range.
        # Therefore, for all invalid ranges we put 0 so it get deleted by rviz
        msg = LaserScan()
        msg.header.frame_id = 'laser_scanner'
        msg.header.stamp = stamp
        msg.angle_min = - 150 * pi / 180
        msg.angle_max = 150 * pi / 180
        msg.angle_increment = 15 * pi / 180
        msg.range_min = 0.005 + SENSOR_DIST_FROM_CENTER
        msg.range_max = 1.0 + SENSOR_DIST_FROM_CENTER
        msg.ranges = [
            dists[3] + SENSOR_DIST_FROM_CENTER,  # -150
            OUT_OF_RANGE,                       # -135
            OUT_OF_RANGE,                       # -120
            OUT_OF_RANGE,                       # -105
            dists[2] + SENSOR_DIST_FROM_CENTER,  # -90
            OUT_OF_RANGE,                       # -75
            OUT_OF_RANGE,                       # -60
            dists[1] + SENSOR_DIST_FROM_CENTER,  # -45
            OUT_OF_RANGE,                       # -30
            dists[0] + SENSOR_DIST_FROM_CENTER,  # -15
            dist_tof + SENSOR_DIST_FROM_CENTER,  # 0
            dists[7] + SENSOR_DIST_FROM_CENTER,  # 15
            OUT_OF_RANGE,                       # 30
            dists[6] + SENSOR_DIST_FROM_CENTER,  # 45
            OUT_OF_RANGE,                       # 60
            OUT_OF_RANGE,                       # 75
            dists[5] + SENSOR_DIST_FROM_CENTER,  # 90
            OUT_OF_RANGE,                       # 105
            OUT_OF_RANGE,                       # 120
            OUT_OF_RANGE,                       # 135
            dists[4] + SENSOR_DIST_FROM_CENTER,  # 150
        ]
        self.laser_publisher.publish(msg)

    def imu_callback(self):
        gyro_data = self.gyro.getValues()
        accelerometer_data = self.accelerometer.getValues()

        msg = Imu()
        msg.angular_velocity.x = gyro_data[0]
        msg.angular_velocity.y = gyro_data[1]
        msg.angular_velocity.z = gyro_data[2]
        msg.linear_acceleration.x = accelerometer_data[0]
        msg.linear_acceleration.y = accelerometer_data[1]
        msg.linear_acceleration.z = accelerometer_data[2]
        self.imu_publisher.publish(msg)

    def led_callback(self, req, res, index):
        self.leds[index].set(req.value)
        res.success = True
        return res

    def camera_callback(self):
        # Image data
        msg = Image()
        msg.height = self.camera.getHeight()
        msg.width = self.camera.getWidth()
        msg.is_bigendian = False
        msg.step = self.camera.getWidth() * 4
        msg.data = self.camera.getImage()
        msg.encoding = 'bgra8'
        self.camera_publisher.publish(msg)

        # CameraInfo data
        msg = CameraInfo()
        msg.header.frame_id = 'camera_frame'
        msg.height = self.camera.getHeight()
        msg.width = self.camera.getWidth()
        msg.distortion_model = 'plumb_bob'
        msg.d = [0.0, 0.0, 0.0, 0.0, 0.0]
        msg.k = [
            self.camera.getFocalLength(), 0.0, self.camera.getWidth() / 2,
            0.0, self.camera.getFocalLength(), self.camera.getHeight() / 2,
            0.0, 0.0, 1.0
        ]
        msg.p = [
            self.camera.getFocalLength(), 0.0, self.camera.getWidth() / 2, 0.0,
            0.0, self.camera.getFocalLength(), self.camera.getHeight() / 2, 0.0,
            0.0, 0.0, 1.0, 0.0
        ]
        self.camera_info_publisher.publish(msg)


def main(args=None):
    rclpy.init(args=args)

    epuck2_controller = EPuckDriver(args=args)

    rclpy.spin(epuck2_controller)
    rclpy.shutdown()


if __name__ == '__main__':
    main()
