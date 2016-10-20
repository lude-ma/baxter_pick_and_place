# Copyright (c) 2016, BRML
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import logging
import numpy as np

from hardware import img_to_imgmsg
from vision import mask_to_rroi, draw_rroi


class Servoing(object):
    def __init__(self, robot, segmentation, pub_vis, object_size, tolerance):
        """Base class for visual servoing. Can be used to position the end
        effector directly over the requested object.
        Note: Assumes that the end effector is restricted to pointing along
            the vertical (z) axis. That is, the object position in the x-y
            plane and the distance to the object along z direction are
            estimated.

        :param robot: A robot abstraction module instance.
        :param segmentation: An object segmentation module instance.
        :param pub_vis: A ROS publisher to publish visualization images with.
        :param object_size: The measured length of the longer dimension of
            each object (in the x-y plane) in meters.
        :param tolerance: The position error tolerance in meters.
        """
        self._robot = robot
        self._segmentation = segmentation
        self._pub_vis = pub_vis
        self._object_size_meters = object_size
        self._tolerance = tolerance

        self._logger = logging.getLogger('main.servo')

    def _find_rotated_enclosing_rect(self, image, object_id):
        """Find the rectangle with arbitrary orientation that encloses the
        segmented object in the given image with minimum area.
        Note: The passed image is being modified and published on the
            visualization image topic!

        :param image: An image (numpy array) of shape (height, width, 3).
        :param object_id: The object identifier.
        :return: The rotated rectangle enclosing the segmented object,
            given by ((cx, cy), (w, h), alpha).
        :raise: ValueError if the given object could not be segmented.
        """
        if object_id == 'hand':
            det = self._segmentation.detect_best(image=image, threshold=0.8)
        else:
            det = self._segmentation.detect_object(image=image,
                                                   object_id=object_id,
                                                   threshold=0.8)
        handstring = ' in hand' if object_id is 'hand' else ''
        if det['mask'] is not None:
            self._logger.info("Segmented {}{}.".format(det['id'], handstring))
            rroi = mask_to_rroi(mask=det['mask'])
        else:
            raise ValueError("Segmentation of {}{} failed!".format(det['id'],
                                                                   handstring))
        draw_rroi(image=image, rroi=rroi)
        self._pub_vis.publish(img_to_imgmsg(image))
        return rroi

    def estimate_distance(self, object_id, rroi, arm):
        """Estimate the distance to the object.

        :param object_id: The object identifier of the object to estimate the
            distance to.
        :param rroi: The rotated rectangle enclosing the segmented object,
            given by ((cx, cy), (w, h), alpha).
        :param arm: The arm <'left', 'right'> to control.
        :return: The approximate distance from the gripper to the object.
        """
        raise NotImplementedError()

    def _pixel_to_camera_factor(self, object_id, rroi, arm):
        """Scale factor mapping from pixels to meters at the current distance.

        :param object_id: The object identifier of the object to estimate the
            distance to.
        :param rroi: The rotated rectangle enclosing the segmented object,
            given by ((cx, cy), (w, h), alpha).
        :param arm: The arm <'left', 'right'> to control.
        :return: The scale factor.
        """
        return (
            self._robot.cameras[arm].meters_per_pixel *
            self.estimate_distance(arm=arm, rroi=rroi, object_id=object_id)
        )

    def _error(self, image_size, object_id, rroi, arm):
        """Convert the offset between the object's center and the image
        center in pixels into the corresponding offset in meters.

        :param image_size: The height and width of the image (h, w).
        :param object_id: The object identifier of the object to estimate the
            distance to.
        :param rroi: The rotated rectangle enclosing the segmented object,
            given by ((cx, cy), (w, h), alpha).
        :param arm: The arm <'left', 'right'> to control.
        :return: The position error in meters.
        """
        h, w = image_size
        pixel_delta = [a - b for a, b in zip((w//2, h//2), rroi[0])]
        pixel_error = np.sqrt(np.dot(pixel_delta, pixel_delta))
        p2c_factor = self._pixel_to_camera_factor(object_id=object_id,
                                                  rroi=rroi, arm=arm)
        return pixel_error*p2c_factor

    def _iterate(self, arm, object_id, rroi):
        """Perform one update of the position of the end effector relative to
        the given object.

        :param arm: The arm <'left', 'right'> to control.
        :param object_id: The object identifier of the object to estimate the
            distance to.
        :param rroi: The rotated rectangle enclosing the segmented object,
            given by ((cx, cy), (w, h), alpha).
        :return: A tuple containing
            - the rotated rectangle enclosing the segmented object, given by
                ((cx, cy), (w, h), alpha) and
            - the position error in meters.
        """
        # TODO: adapt control parameter
        kp = 0.7  # proportional control parameter

        # TODO: servoing should not only update x and y, but also z!
        img = self._robot.cameras[arm].collect_image()
        camera_error = self._error(image_size=img.shape[:2],
                                   object_id=object_id, rroi=rroi, arm=arm)
        if camera_error > self._tolerance:
            p2c_factor = self._pixel_to_camera_factor(object_id=object_id,
                                                      rroi=rroi, arm=arm)
            h, w = img.shape[:2]
            dx, dy = [(b - a)*p2c_factor*kp
                      for a, b in zip((w//2, h//2), rroi[0])]
            pose = self._robot.endpoint_pose(arm=arm)
            # TODO: verify this update works as expected
            pose = [a + b for a, b in zip(pose, [dx, dy, 0, 0, 0, rroi[2]])]
            try:
                cfg = self._robot.inverse_kinematics(arm=arm, pose=pose)
                self._robot.move_to(config=cfg)
                rroi = self._find_rotated_enclosing_rect(image=img,
                                                         object_id=object_id)
                camera_error = self._error(image_size=img.shape[:2],
                                           object_id=object_id, rroi=rroi,
                                           arm=arm)
            except ValueError as e:
                self._logger.error(e)
        return rroi, camera_error

    def servo(self, arm, object_id):
        """Apply visual servoing to position the end effector over the given
        object.

        :param arm: The arm <'left', 'right'> to control.
        :param object_id: The object identifier of the object to servo to.
        :return: boolean success value.
        """
        img = self._robot.camears[arm].collect_image()
        try:
            rroi = self._find_rotated_enclosing_rect(image=img,
                                                     object_id=object_id)
        except ValueError as e:
            self._logger.error(e)
            return False
        camera_error = 2*self._tolerance
        it = 1
        while camera_error > self._tolerance:
            self._logger.debug("Iteration {} finished".format(it))
            it += 1
            rroi, camera_error = self._iterate(arm=arm, object_id=object_id,
                                               rroi=rroi)
        return True
