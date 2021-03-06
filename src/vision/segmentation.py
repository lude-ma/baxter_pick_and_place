#!/usr/bin/env python

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
import os
import time

import cv2

from init_paths import set_up_mnc
set_up_mnc()
# suppress caffe logging up to 0 debug, 1 info 2 warning 3 error
os.environ['GLOG_minloglevel'] = '2'
import caffe as caffe_mnc
from mnc_config import cfg
from transform.bbox_transform import clip_boxes
from utils.blob import prep_im_for_blob, im_list_to_blob


class ObjectSegmentation(object):
    def __init__(self, root_dir, object_ids):
        """Instantiates a 'faster R-CNN' object detector and segmentation object.

        :param root_dir: Where the baxter_pick_and_place ROS package resides.
        :param object_ids: The list of object identifiers in the set of
            objects. Needs to be
            [background, object 1, object 2, ..., object N].
        """
        self._classes = object_ids

        self._logger = logging.getLogger('main.mnc')

        self._net = None
        self._prototxt = os.path.join(root_dir, 'models', 'VGG16',
                                      'mnc_5stage_test.pt')
        self._caffemodel = os.path.join(root_dir, 'data', 'VGG16',
                                        'mnc_model.caffemodel.h5')
        if not os.path.isfile(self._prototxt):
            raise RuntimeError("No network architecture specification found "
                               "at %s!" % self._prototxt)
        if not os.path.isfile(self._caffemodel):
            raise RuntimeError("No network parameter dump found at %s!" %
                               self._caffemodel)

    def _prepare_mnc_args(self, image):
        """Taken from
        https://github.com/daijifeng001/MNC/blob/master/tools/demo.py.
        I have no idea what this does.

        :param image: An image (numpy array) of shape (height, width, 3).
        :return: Whatever, I have no idea.
        """
        # Prepare image data blob
        blobs = {'data': None}
        processed_ims = []
        image, im_scale_factors = \
            prep_im_for_blob(image, cfg.PIXEL_MEANS, cfg.TEST.SCALES[0], cfg.TRAIN.MAX_SIZE)
        processed_ims.append(image)
        blobs['data'] = im_list_to_blob(processed_ims)
        # Prepare image info blob
        im_scales = [np.array(im_scale_factors)]
        assert len(im_scales) == 1, 'Only single-image batch implemented'
        im_blob = blobs['data']
        blobs['im_info'] = np.array(
            [[im_blob.shape[2], im_blob.shape[3], im_scales[0]]],
            dtype=np.float32)
        # Reshape network inputs and do forward
        self._net.blobs['data'].reshape(*blobs['data'].shape)
        self._net.blobs['im_info'].reshape(*blobs['im_info'].shape)
        forward_kwargs = {
            'data': blobs['data'].astype(np.float32, copy=False),
            'im_info': blobs['im_info'].astype(np.float32, copy=False)
        }
        return forward_kwargs, im_scales

    def _im_detect(self, image):
        """Taken from
        https://github.com/daijifeng001/MNC/blob/master/tools/demo.py.
        Somehow combines different stages of the network. No idea how it works.

        :param image: An image (numpy array) of shape (height, width, 3).
        :return: A tuple of three numpy arrays, the n_proposals x n_classes
            scores, the corresponding n_proposals x 4 bounding boxes, where
            each bounding box is defined as <xul, yul, xlr, ylr> and the
            n_proposals x 1 x 21 x 21 segmentation masks.
        """
        forward_kwargs, im_scales = self._prepare_mnc_args(image)
        blobs_out = self._net.forward(**forward_kwargs)
        # output we need to collect:
        # 1. output from phase1'
        rois_phase1 = self._net.blobs['rois'].data.copy()
        masks_phase1 = self._net.blobs['mask_proposal'].data[...]
        scores_phase1 = self._net.blobs['seg_cls_prob'].data[...]
        # 2. output from phase2
        rois_phase2 = self._net.blobs['rois_ext'].data[...]
        masks_phase2 = self._net.blobs['mask_proposal_ext'].data[...]
        scores_phase2 = self._net.blobs['seg_cls_prob_ext'].data[...]
        # Boxes are in resized space, we un-scale them back
        rois_phase1 = rois_phase1[:, 1:5] / im_scales[0]
        rois_phase2 = rois_phase2[:, 1:5] / im_scales[0]
        rois_phase1, _ = clip_boxes(rois_phase1, image.shape)
        rois_phase2, _ = clip_boxes(rois_phase2, image.shape)
        # concatenate two stages to get final network output
        masks = np.concatenate((masks_phase1, masks_phase2), axis=0)
        boxes = np.concatenate((rois_phase1, rois_phase2), axis=0)
        scores = np.concatenate((scores_phase1, scores_phase2), axis=0)
        return scores, boxes, masks

    def init_model(self, warmup=False):
        """Load the pre-trained Caffe model onto GPU0.

        :param warmup: Whether to warm up the model on some dummy images.
        :return:
        """
        caffe_mnc.set_mode_gpu()
        gpu_id = 0
        caffe_mnc.set_device(gpu_id)
        cfg.GPU_ID = gpu_id

        self._net = caffe_mnc.Net(self._prototxt, self._caffemodel, caffe_mnc.TEST)
        self._logger.info('Loaded network %s.' % self._caffemodel)
        if warmup:
            self._logger.debug('Warming up on dummy images.')
            dummy = 128 * np.ones((300, 500, 3), dtype=np.uint8)
            for _ in xrange(2):
                _, _, _ = self._im_detect(dummy)

    def detect(self, image):
        """Feed forward the given image through the previously loaded network.
        Return scores, bounding boxes and segmentation masks for all abject
        proposals and classes.

        :param image: An image (numpy array) of shape (height, width, 3).
        :return: A tuple of three numpy arrays, the n_proposals x n_classes
            scores, the corresponding n_proposals x 4 bounding boxes, where
            each bounding box is defined as <xul, yul, xlr, ylr> and the
            n_proposals x 1 x 21 x 21 segmentation masks.
        """
        if self._net is None:
            raise RuntimeError("No loaded network found! "
                               "Did you run init_model()?")
        if len(image.shape) != 3 and image.shape[2] != 3:
            raise ValueError("Image must be a three channel color image "
                             "with shape (h, w, 3)!")
        start = time.time()
        scores, boxes, masks = self._im_detect(image)
        self._logger.debug('Detection took {:.3f}s for {:d} object proposals'.format(
            time.time() - start, boxes.shape[0])
        )
        return scores, boxes, masks

    @staticmethod
    def _mask_image_from_mask(mask, box, image_size):
        """Convert an MNC mask detection into a binary mask image.

        :param mask: The MNC mask detection to convert (a 21x21 numpy array).
        :param box: The corresponding bounding box of the detection.
        :param image_size: The height and width of the input and mask images.
        :return: The segmentation of the detection; a (height, width) numpy
            array.
        """
        def clip_box(box, image_size):
            """Clip bounding box to image size.

            :param box: The bounding box, defined as <xul, yul, xlr, ylr>.
            :param image_size: The height and width of the image.
            :return: The bounding box coordinates, clipped to the image size
                and rounded to the next integer value.
            """
            # clip box into image space
            box = np.round(box).astype(np.uint32)
            height, width = image_size
            box[0] = min(max(box[0], 0), width - 1)
            box[1] = min(max(box[1], 0), height - 1)
            box[2] = min(max(box[2], 0), width - 1)
            box[3] = min(max(box[3], 0), height - 1)
            return box

        height, width = image_size
        mask_img = np.zeros((height, width), dtype=np.uint8)
        box = clip_box(box=box, image_size=image_size)
        mask = cv2.resize(mask, (box[2] - box[0] + 1, box[3] - box[1] + 1))
        mask = mask >= cfg.BINARIZE_THRESH
        mask = 255*np.array(mask, dtype=np.uint8)
        mask_img[box[1]:box[3] + 1, box[0]:box[2] + 1] = mask
        return mask_img

    def detect_object(self, image, object_id, threshold=0.5):
        """Feed forward the given image through the previously loaded network.
        Return the bounding box and segmentation with the highest score for
        the requested object class.

        :param image: An image (numpy array) of shape (height, width, 3).
        :param object_id: One object identifier string contained in the list
            of objects.
        :param threshold: The threshold (0, 1) on the score for a detection
            to be considered as valid.
        :return: A dictionary containing the detection with
            'id': The object identifier.
            'score: The score of the detection (scalar).
            'box': The bounding box of the detection; a (4,) numpy array.
            'mask': The segmentation of the detection; a (height, width)
                numpy array.
        """
        if object_id not in self._classes:
            raise KeyError("Object {} is not contained in the defined "
                           "set of objects!".format(object_id))
        scores, boxes, masks = self.detect(image=image)

        # Find scores for requested object class
        cls_idx = self._classes.index(object_id)
        cls_scores = scores[:, cls_idx]

        best_idx = np.argmax(cls_scores)
        best_score = cls_scores[best_idx]
        best_box = boxes[best_idx]
        best_mask = self._mask_image_from_mask(mask=masks[best_idx][0],
                                               box=best_box,
                                               image_size=image.shape[:2])
        self._logger.debug('Best score for {} is {:.3f} {} {:.3f}'.format(
            object_id,
            best_score,
            '>=' if best_score > threshold else '<',
            threshold)
        )
        if best_score > threshold:
            return {'id': object_id, 'score': best_score, 'box': best_box, 'mask': best_mask}
        return {'id': object_id, 'score': best_score, 'box': None, 'mask': None}

    def detect_best(self, image, threshold=0.5):
        """Feed forward the given image through the previously loaded network.
        Return the bounding box and segmentation with the highest score
        amongst all classes.

        :param image: An image (numpy array) of shape (height, width, 3).
        :param threshold: The threshold (0, 1) on the score for a detection
            to be considered as valid.
        :return: A dictionary containing the detection with
            'id': The object identifier.
            'score: The score of the detection (scalar).
            'box': The bounding box of the detection; a (4,) numpy array.
            'mask': The segmentation of the detection; a (height, width)
                numpy array.
        """
        scores, boxes, masks = self.detect(image=image)

        # find best score among all classes (except background)
        best_proposal, best_class = np.unravel_index(scores[:, 1:].argmax(),
                                                     scores[:, 1:].shape)
        best_class += 1  # compensate for background
        best_score = scores[best_proposal, best_class]
        best_box = boxes[best_proposal]
        best_mask = self._mask_image_from_mask(mask=masks[best_proposal][0],
                                               box=best_box,
                                               image_size=image.shape[:2])
        best_object = self._classes[best_class]

        self._logger.debug('Best score for {} is {:.3f} {} {:.3f}'.format(
            best_object,
            best_score,
            '>=' if best_score > threshold else '<',
            threshold)
        )
        if best_score > threshold:
            return {'id': best_object, 'score': best_score, 'box': best_box, 'mask': best_mask}
        return {'id': best_object, 'score': best_score, 'box': None, 'mask': None}


if __name__ == '__main__':
    from visualization_utils import draw_detection
    path = '/home/mludersdorfer/software/ws_baxter_pnp/src/baxter_pick_and_place'
    print 'started'
    classes = ('__background__',
               'aeroplane', 'bicycle', 'bird', 'boat',
               'bottle', 'bus', 'car', 'cat', 'chair',
               'cow', 'diningtable', 'dog', 'horse',
               'motorbike', 'person', 'pottedplant',
               'sheep', 'sofa', 'train', 'tvmonitor')
    od = ObjectSegmentation(root_dir=path, object_ids=classes)
    od.init_model(warmup=False)

    for img_file in [os.path.join(path, 'data', '%s.jpg' % i)
                     for i in ['2008_000533', '2008_000910', '2008_001602',
                               '2008_001717', '2008_008093']]:
        img = cv2.imread(img_file)
        if img is not None:
            # det = od.detect_best(img, 0.8)
            det = od.detect_object(img, 'person', 0.8)
            if det['box'] is not None:
                draw_detection(img, det)
                cv2.imshow('image', img)
                cv2.waitKey(0)
    cv2.destroyAllWindows()
    print 'ended'
