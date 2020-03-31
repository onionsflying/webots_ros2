from math import cos, sin
from geometry_msgs.msg import Quaternion


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


def interpolate_function(value, start_x, start_y, end_x, end_y):
    slope = (end_y - start_y) / (end_x - start_x)
    return slope * (value - start_x) + start_y


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