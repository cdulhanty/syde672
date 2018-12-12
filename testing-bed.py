'''

from sort import *

#create instance of SORT
mot_tracker = Sort()

# get detections
...

# update SORT
track_bbs_ids = mot_tracker.update(detections)

# track_bbs_ids is a np array where each row contains a valid bounding box and track_id (last column)
...
'''
