#!/usr/bin/env python3
import numpy as np
import rospy

from std_msgs.msg import Float32MultiArray        # See https://gist.github.com/jarvisschultz/7a886ed2714fac9f5226
from geometry_msgs.msg import Vector3, Twist
from setting_params import FREQ_LOW_LEVEL

from utils.kalman_filter import Tracker

USE_KALMAN = True

### ROS Subscriber Callback ###
STATE_ARRAY_RECEIVED = None
def fnc_callback(msg):
    global STATE_ARRAY_RECEIVED
    STATE_ARRAY_RECEIVED = msg

PREDICTION_ARRAY_RECEIVED = None
def fnc_callback1(msg):
    global PREDICTION_ARRAY_RECEIVED
    PREDICTION_ARRAY_RECEIVED = msg

P_gain = .5
TARGET_CLASS_INDEX = 2


if __name__=='__main__':

    # rosnode node initialization
    rospy.init_node('car_controller_node')

    # subscriber init.
    sub  = rospy.Subscriber('/airsim_node/state_obs_values', Float32MultiArray, fnc_callback)
    sub1 = rospy.Subscriber('/yolo_node/yolo_predictions', Float32MultiArray, fnc_callback1)

    # publishers init.
    pub_vel_est = rospy.Publisher('/controller_node/vel_est_rcvd', Vector3, queue_size=10)
    pub_body_angle = rospy.Publisher('/controller_node/body_angle_rcvd', Vector3, queue_size=10)
    pub_tgt_box = rospy.Publisher('/controller_node/tgt_box_rcvd', Vector3, queue_size=10)
    pub_vel_cmd = rospy.Publisher('/controller_node/vel_cmd', Vector3, queue_size=10)
    pub_kf_box = rospy.Publisher('/controller_node/kf_box', Twist, queue_size=10)

    # Running rate
    rate=rospy.Rate(FREQ_LOW_LEVEL)

    # msg init.
    vel_est    = Vector3()
    body_angle = Vector3()
    tgt_box    = Vector3()
    vel_cmd_tracking = Vector3()
    kf_box = Twist()

    t_step = 0


    # Kalman filter tracker
    tracker = Tracker()

    

    n_prediction_in_row = 0

    ##############################
    ### Instructions in a loop ###
    ##############################
    while not rospy.is_shutdown():

        t_step += 1

        if STATE_ARRAY_RECEIVED is not None:

            height = STATE_ARRAY_RECEIVED.layout.dim[0].size
            width = STATE_ARRAY_RECEIVED.layout.dim[1].size
            np_state = np.array(STATE_ARRAY_RECEIVED.data).reshape((height, width))

            pitch, roll, yaw = (np_state[0][1], np_state[0][2], np_state[0][0])
            vgx, vgy, vgz = (np_state[1][0], np_state[1][1], np_state[1][2])
            x, y, z = (np_state[2][0], np_state[2][1], np_state[2][2])
            
            vel_est.x, vel_est.y, vel_est.z = (vgx, vgy, vgz)
            body_angle.x, body_angle.y, body_angle.z = (pitch, roll, yaw)

            ##########################
            ### Kalman Filter ###
            ##########################
            if PREDICTION_ARRAY_RECEIVED is not None:
                height = PREDICTION_ARRAY_RECEIVED.layout.dim[0].size
                width = PREDICTION_ARRAY_RECEIVED.layout.dim[1].size
                np_prediction = np.array(PREDICTION_ARRAY_RECEIVED.data).reshape((height, width))

                ### Publish the prediction results in results.xyxy[0]) ###
                #                   x1           y1           x2           y2   confidence        class
                # tensor([[7.50637e+02, 4.37279e+01, 1.15887e+03, 7.08682e+02, 8.18137e-01, 0.00000e+00],
                #         [9.33597e+01, 2.07387e+02, 1.04737e+03, 7.10224e+02, 5.78011e-01, 0.00000e+00],
                #         [4.24503e+02, 4.29092e+02, 5.16300e+02, 7.16425e+02, 5.68713e-01, 2.70000e+01]])
                if np_prediction.shape[0] == 1 and np_prediction.shape[1] == 1:
                    print("No detection")
                    tracker.predict_only() # Predict KF
                else:
                    tgt_boxes = np_prediction[np.where(np_prediction[:,5]==TARGET_CLASS_INDEX)]

                    if len(tgt_boxes) > 0:

                        x_min = tgt_boxes[:,0]
                        y_min = tgt_boxes[:,1]
                        x_max = tgt_boxes[:,2]
                        y_max = tgt_boxes[:,3]

                        z = np.array([x_min, y_min, x_max, y_max])

                        tracker.kalman_filter(z.T[0]) # Update KF
                        n_prediction_in_row = 0

                    else:
                        tracker.predict_only() # Predict KF                        
                        n_prediction_in_row += 1
            else:
                tracker.predict_only() # Predict KF
                n_prediction_in_row += 1
        else:
            tracker.predict_only() # Predict KF
            n_prediction_in_row += 1

        ########################
        ### Generate Control ###
        ########################
        xmin, xmin_dot, ymin, ymin_dot, xmax, xmax_dot, ymax, ymax_dot = tracker.x_state

        kf_box.linear.x = xmin
        kf_box.linear.y = ymin
        kf_box.linear.z = xmax
        kf_box.angular.x = ymax
        kf_box.angular.y = 0
        kf_box.angular.z = 0

        x_ctr = (xmin + xmax)/2/100
        y_ctr = (ymin + ymax)/2/100
        size  = (ymax - ymin)*(xmax - xmin)/1000

        # -------------------------------#
        #|             y = 1
        #|
        #|
        #| x = 0      (5, 3.5)      x = 10
        #|
        #|
        #|             y = 6
        # -------------------------------#

        # size [10, 100]   target 30
        CTR_X_POS = 2.4
        CTR_Y_POS = 3.1
        AREA_TGT = 30

        
        # Take the most believable bounding box if there are multiple of them.
        tgt_box.x, tgt_box.y, tgt_box.z = (float(x_ctr), float(y_ctr), float(size))

        ### Now, make the control signal ###
        error_x = tgt_box.x - CTR_X_POS
        error_y = tgt_box.y - CTR_Y_POS
        error_z = (tgt_box.z)**0.5 - (AREA_TGT)**0.5

        cmd_vx =  P_gain * error_x*0.5    # side move (left or right)
        cmd_vy = -P_gain * error_y   # vertical move (up or down)
        cmd_vz = -P_gain * error_z*0.7   # front move (front or backward)

        if n_prediction_in_row > 5:
            cmd_vx = 0
            cmd_vy = 0
            cmd_vz = 0

        vel_cmd_tracking.x = cmd_vx  # if target is at the right then generate positive cmd_vx
        vel_cmd_tracking.y = cmd_vy  # if target is at the above then generate positive cmd_vy
        vel_cmd_tracking.z = cmd_vz  # if target is small then generate positive cmd_vz

        ### Publish ###
        #print('vel_est', vel_est)
        pub_vel_est.publish(vel_est)
        #print('body_angle', body_angle)
        pub_body_angle.publish(body_angle)
        #print('tgt_box', tgt_box)
        pub_tgt_box.publish(tgt_box)
        #print('vel_cmd_tracking', vel_cmd_tracking)
        pub_vel_cmd.publish(vel_cmd_tracking)
        #print('kf_box', kf_box)
        pub_kf_box.publish(kf_box)

        try:
            experiment_done_done = rospy.get_param('experiment_done')
        except:
            experiment_done_done = False

        if experiment_done_done and t_step > FREQ_LOW_LEVEL*3:
            print('experiment_done_done')
            rospy.signal_shutdown('Finished 100 Episodes!')


        rate.sleep()