"""
SORT: A Simple, Online and Realtime Tracker
Copyright (C) 2016 Alex Bewley alex@dynamicdetection.com

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import print_function

from numba import jit  # python compiler - makes pyhton code run fasst! (gotta go fast)
import os.path
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as patches
from numpy.core.multiarray import ndarray
from skimage import io
from sklearn.utils.linear_assignment_ import \
    linear_assignment  # Solve the unique lowest-cost assignment problem using the Hungarian algorithm
import time
import argparse
import json
from keras.layers import Input, LSTM, Dense
from keras.models import Model, load_model

@jit
def iou(bb_test: object, bb_gt: object) -> object:
    """
    Computes IUO between two bboxes in the form [x1,y1,x2,y2]
    """
    xx1 = np.maximum(bb_test[0], bb_gt[0])
    yy1 = np.maximum(bb_test[1], bb_gt[1])
    xx2 = np.minimum(bb_test[2], bb_gt[2])
    yy2 = np.minimum(bb_test[3], bb_gt[3])
    w = np.maximum(0., xx2 - xx1)
    h = np.maximum(0., yy2 - yy1)
    wh = w * h
    o = wh / ((bb_test[2] - bb_test[0]) * (bb_test[3] - bb_test[1])
              + (bb_gt[2] - bb_gt[0]) * (bb_gt[3] - bb_gt[1]) - wh)
    return o


def convert_bbox_to_z(bbox):
    """
    Takes a bounding box in the form [x1,y1,x2,y2] and returns z in the form
    [x,y,s,r] where x,y is the centre of the box and s is the scale/area and r is
    the aspect ratio
    """
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x = bbox[0] + w / 2.
    y = bbox[1] + h / 2.
    s = w * h  # scale is just area
    r = w / float(h)
    return np.array([x, y, s, r]).reshape((4, 1))


def convert_x_to_bbox(x, score=None):
    """
    Takes a bounding box in the centre form [x,y,s,r] and returns it in the form
    [x1,y1,x2,y2] where x1,y1 is the top left and x2,y2 is the bottom rights
    """
    w = np.sqrt(x[2] * x[3])
    h = x[2] / w
    if score is None:
        return np.array([x[0] - w / 2., x[1] - h / 2., x[0] + w / 2., x[1] + h / 2.]).reshape((1, 4))
    else:
        return np.array([x[0] - w / 2., x[1] - h / 2., x[0] + w / 2., x[1] + h / 2., score]).reshape((1, 5))


def convert_bboxs_to_lstm(bboxs, height, max_height, width, max_width, fps, max_fps, score=None):
    """
    Takes a bounding box in the form [x1,y1,x2,y2] and returns it in the form
    [norm_bbox_x1, norm_bbox_y1, norm_bbox_width, norm_bbox_height, norm_seq_fps, norm_seq_width, norm_seq_height]
    where x1, y1 is the top left
    """

    # TODO - check for no bounding boxes
    lstm_inputs = np.array([bboxs[:, 0] / float(width),
                            bboxs[:, 1] / float(height),
                            (bboxs[:, 2] - bboxs[:, 0]) / float(width),
                            (bboxs[:, 3] - bboxs[:, 1]) / float(height),
                            [fps / max_fps for i in range(len(bboxs))],
                            [width / max_width for i in range(len(bboxs))],
                            [height / max_height for i in range(len(bboxs))]])

    return lstm_inputs.T


def convert_lstm_to_bbox(lstm_input, height, width, score=None):
    """
    Takes a lstm_input in the form
    [norm_bbox_x1, norm_bbox_y1, norm_bbox_width, norm_bbox_height, norm_seq_fps, norm_seq_width, norm_seq_height]
    and returns it in the form [x1,y1,x2,y2] where x1,y1 is the top left and x2,y2 is the bottom rights
    """
    x1 = lstm_input[0] * width
    y1 = lstm_input[1] * height
    bbox_width = lstm_input[2] * width
    bbox_height = lstm_input[3] * height

    x2 = x1 + bbox_width
    y2 = y1 + bbox_height

    return np.array([x1, y1, x2, y2])


class LSTMTracker(object):
    """
    This class represents the internal state of individual tracked objects observed as bounding boxes
    """
    count = 0

    def __init__(self, bbox, model):  # instantiate the LSTM with first bounding box detection
        """
        Initialises a tracker using initial bounding box.
        """

        self.history = []
        self.history.append(bbox)

        self.model = model

        self.time_since_update = 0
        self.id = LSTMTracker.count
        LSTMTracker.count += 1

        self.hits = 0
        self.hit_streak = 0
        self.age = 0

    def update(self, bbox):
        """
        Updates the state vector with observed bbox.
        """
        self.time_since_update = 0
        self.hits += 1
        self.hit_streak += 1
        self.history[-1] = bbox  # TODO TBC: replace last prediction with the bbox that was connected? an algo. Q

    def predict(self):
        """
        Advances the state vector and returns the predicted bounding box estimate.
        """

        self.age += 1

        if self.time_since_update > 0:
            self.hit_streak = 0

        self.time_since_update += 1

        pred = self.model.predict(np.expand_dims(np.array(self.history), 0))  # run predict on LSTM model

        pred_as_list = pred[0].tolist()
        # append normalized_seq info to the prediction
        full_lstm_list = pred_as_list + [self.history[0][-3], self.history[0][-2], self.history[0][-1]]

        self.history.append(full_lstm_list)

        return self.history[-1]

    def get_state(self):
        """
        Returns the current bounding box estimate.
        """
        return self.history[-1]


# noinspection PyTypeChecker
def associate_detections_to_trackers(detections, tracked_objects, iou_threshold=0.3):
    """
    Assigns detections to tracked object (both represented as bounding boxes)

    Returns 3 lists of matches, unmatched_detections and unmatched_trackers
    """
    if len(tracked_objects) == 0:
        return np.empty((0, 2), dtype=int), np.arange(len(detections)), np.empty((0, 5), dtype=int)
    iou_matrix = np.zeros((len(detections), len(tracked_objects)), dtype=np.float32)  # type: ndarray

    for d_index, det in enumerate(detections):
        for t, trk in enumerate(tracked_objects):
            iou_matrix[d_index, t] = iou(det, trk)

    matched_indices = linear_assignment(-iou_matrix)  # call to the Hungarian algo here

    unmatched_detections = []
    for d_index, det in enumerate(detections):
        if d_index not in matched_indices[:, 0]:
            unmatched_detections.append(d_index)

    unmatched_trackers = []
    for t, trk in enumerate(tracked_objects):
        if t not in matched_indices[:, 1]:
            unmatched_trackers.append(t)

    # filter out matched with low IOU
    matches = []
    for m in matched_indices:
        if iou_matrix[m[0], m[1]] < iou_threshold:
            unmatched_detections.append(m[0])
            unmatched_trackers.append(m[1])
        else:
            matches.append(m.reshape(1, 2))
    if len(matches) == 0:
        matches = np.empty((0, 2), dtype=int)
    else:
        matches = np.concatenate(matches, axis=0)

    return matches, np.array(unmatched_detections), np.array(unmatched_trackers)


class Sort(object):  # main class
    def __init__(self, height, max_height, width, max_width, fps, max_fps, model, max_age=1, min_hits=3):

        """
        Sets key parameters for SORT
        """
        self.height = height
        self.max_height = max_height

        self.width = width
        self.max_width = max_width

        self.fps = fps
        self.max_fps = max_fps

        self.model = model

        self.max_age = max_age
        self.min_hits = min_hits
        self.trackers = []
        self.frame_count = 0

    def update(self, detections):

        """
        Params:
        dets - a numpy array of detections in the format [[x1,y1,x2,y2,score],[x1,y1,x2,y2,score],...]
        Requires: this method must be called once for each frame even with empty detections.
        Returns the a similar array, where the last column is the object ID.

        NOTE: The number of objects returned may differ from the number of detections provided.
        """

        self.frame_count += 1

        # PREDICT STEP
        # get predicted locations from existing trackers.
        trks = np.zeros((len(self.trackers), 5))
        to_del = []
        ret = []

        for t, trk in enumerate(trks):

            # call to state estimation method
            normalized_pred = self.trackers[t].predict()
            pred = convert_lstm_to_bbox(normalized_pred, self.height, self.width)  # convert to [x1,y1,x2,y2] form

            trk[:] = [pred[0], pred[1], pred[2], pred[3], 0]

            # TODO check for negative numbers as a result of the LSTM :/
            if np.any(np.isnan(pred)):
                to_del.append(t)

        trks = np.ma.compress_rows(np.ma.masked_invalid(trks))

        for t in reversed(to_del):
            self.trackers.pop(t)

        # Bipartite matching
        matched, unmatched_dets, unmatched_trks = associate_detections_to_trackers(detections, trks)
        # TODO - update for 0 detections in the frame
        lstm_detections = convert_bboxs_to_lstm(detections, self.height, self.max_height, self.width,
                                                self.max_width, self.fps, self.max_fps)

        # UPDATE STEP
        # update matched trackers with assigned detections
        for t, trk in enumerate(self.trackers):
            if t not in unmatched_trks:
                det = matched[np.where(matched[:, 1] == t)[0], 0]
                trk.update(lstm_detections[det, :][0])

        # CREATE STEP
        # create and initialise new trackers for unmatched detections
        for i in unmatched_dets:
            trk = LSTMTracker(lstm_detections[i, :], self.model)
            self.trackers.append(trk)

        i = len(self.trackers)
        for trk in reversed(self.trackers):
            lstm_state = trk.get_state()
            det = convert_lstm_to_bbox(lstm_state, self.height, self.width)  # convert to [x1,y1,x2,y2] form
            if (trk.time_since_update < 1) and (trk.hit_streak >= self.min_hits or self.frame_count <= self.min_hits):
                ret.append(np.concatenate((det, [trk.id + 1])).reshape(1, -1))  # +1 as MOT benchmark requires positive
            i -= 1
            # remove dead tracklet
            if trk.time_since_update > self.max_age:
                self.trackers.pop(i)
        if len(ret) > 0:
            return np.concatenate(ret)
        return np.empty((0, 5))


def parse_args():
    """Parse input arguments."""
    parser = argparse.ArgumentParser(description='SORT demo')
    parser.add_argument('--display', dest='display', help='Display online tracker output (slow) [False]',
                        action='store_true')
    argus = parser.parse_args()
    return argus


if __name__ == '__main__':
    # all train
    '''
    sequences = ['PETS09-S2L1', 'TUD-Campus', 'TUD-Stadtmitte', 'ETH-Bahnhof', 'ETH-Sunnyday', 'ETH-Pedcross2',
                 'KITTI-13', 'KITTI-17', 'ADL-Rundle-6', 'ADL-Rundle-8', 'Venice-2']
    '''
    sequences = ['PETS09-S2L1']
    args = parse_args()
    display = args.display
    phase = 'train'

    with open('mot_benchmark.json') as f:  # load metadata
        metadata = json.load(f)

    max_width = 0
    max_height = 0
    max_fps = 0
    for seq in sequences:
        if metadata[seq]['height'] > max_height:
            max_height = metadata[seq]['height']
        if metadata[seq]['width'] > max_width:
            max_width = metadata[seq]['width']
        if metadata[seq]['fps'] > max_fps:
            max_fps = metadata[seq]['fps']

    total_time = 0.0
    total_frames = 0
    colours = np.random.rand(32, 3)  # used only for display
    if display:
        if not os.path.exists('mot_benchmark'):
            print(
                '\n\tERROR: mot_benchmark link not found!\n\n    Create a symbolic link to the MOT benchmark\n    ('
                'https://motchallenge.net/data/2D_MOT_2015/#download). E.g.:\n\n    $ ln -s '
                '/path/to/MOT2015_challenge/2DMOT2015 mot_benchmark\n\n')
            exit()
        plt.ion()
        fig = plt.figure()

    if not os.path.exists('lstm_output'):
        os.makedirs('lstm_output')

    for seq in sequences:

        # load metadata of video sequence
        seq_height = metadata[seq]['height']
        seq_width = metadata[seq]['width']
        seq_fps = metadata[seq]['fps']
        #seq_weights_path = seq + '.h5'
        seq_weights_path = 'PETS09-S2L1.h5'

        # construct the LSTM architecture TODO architecture may be changed!
        inputs = Input(shape=(None, 7), name='inputs')
        x = LSTM(32)(inputs)
        outputs = Dense(4)(x)
        seq_model = Model(inputs=inputs, outputs=outputs)

        # load the correct LSTM weights into memory, trained on the other sequences
        seq_model.load_weights(seq_weights_path)
        seq_model.compile(loss='mean_squared_error', optimizer='adam')

        mot_tracker = Sort(seq_height, max_height, seq_width, max_width, seq_fps, max_fps, seq_model)  # create instance of the SORT tracker
        seq_dets = np.loadtxt('data/%s/det.txt' % seq, delimiter=',')  # load detections

        with open('lstm_output/%s.txt' % seq, 'w') as out_file:
            print("Processing %s." % seq)

            for frame in range(int(seq_dets[:, 0].max())):
                frame += 1  # detection and frame numbers begin at 1
                dets = seq_dets[seq_dets[:, 0] == frame, 2:7]  # [x1, y1, w, h, score]
                dets[:, 2:4] += dets[:, 0:2]  # convert from [x1, y1, w, h] to [x1, y1, x2, y2]
                total_frames += 1

                if display:
                    ax1 = fig.add_subplot(111, aspect='equal')
                    fn = 'mot_benchmark/%s/%s/img1/%06d.jpg' % (phase, seq, frame)
                    im = io.imread(fn)
                    ax1.imshow(im)
                    plt.title(seq + ' Tracked Targets')

                start_time = time.time()
                trackers = mot_tracker.update(dets)  # multiple detections in the form [x1, y1, x2, y2]
                cycle_time = time.time() - start_time
                total_time += cycle_time

                for d in trackers:
                    print('%d,%d,%.2f,%.2f,%.2f,%.2f,1,-1,-1,-1' % (frame, d[4], d[0], d[1], d[2] - d[0], d[3] - d[1]),
                          file=out_file)
                    if display:
                        d = d.astype(np.int32)
                        ax1.add_patch(patches.Rectangle((d[0], d[1]), d[2] - d[0], d[3] - d[1], fill=False, lw=3,
                                                        ec=colours[d[4] % 32, :]))
                        ax1.set_adjustable('box-forced')

                if display:
                    fig.canvas.flush_events()
                    plt.draw()
                    ax1.cla()

        del seq_model

    print("Total Tracking took: %.3f for %d frames or %.1f FPS" % (total_time, total_frames, total_frames / total_time))

    if display:
        print("Note: to get real runtime results run without the option: --display")
