from __future__ import absolute_import
from __future__ import division

from typing import List
import json
import cv2
import math
import pyclipper
from shapely.geometry import Polygon
import numpy as np
from PIL import Image

from .iaa_augment import IaaAugment

from mindcv.data.constants import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD

from .mz_db_transforms import MZIrregularNormToCHW, MZResizeByGrid, MZRandomScaleByShortSide, MZRandomCropData, MZMakeSegDetectionData, MZMakeBorderMap, MZRandomColorAdjust, MZScalePad

from .random_crop_data import EastRandomCropData
#IMAGENET_DEFAULT_MEAN = [0.485 * 255, 0.456 * 255, 0.406 * 255]
#IMAGENET_DEFAULT_STD = [0.229 * 255, 0.224 * 255, 0.225 * 255]

# default mean/std from ImageNet in RGB order
DEFAULT_MEAN = [0.485, 0.456, 0.406]
DEFAULT_STD = [0.229, 0.224, 0.225]

def transform(data, ops=None):
    """ transform """
    if ops is None:
        ops = []
    for op in ops:
        data = op(data)
        if data is None:
            return None
    return data


class MakeBorderMap(object):
    def __init__(self,
                 shrink_ratio=0.4,
                 thresh_min=0.3,
                 thresh_max=0.7,
                 **kwargs):
        self.shrink_ratio = shrink_ratio
        self.thresh_min = thresh_min
        self.thresh_max = thresh_max

    def __call__(self, data):

        img = data['image']
        text_polys = data['polys']
        ignore_tags = data['ignore_tags']

        canvas = np.zeros(img.shape[:2], dtype=np.float32)
        mask = np.zeros(img.shape[:2], dtype=np.float32)

        for i in range(len(text_polys)):
            if ignore_tags[i]:
                continue
            self.draw_border_map(text_polys[i], canvas, mask=mask)
        canvas = canvas * (self.thresh_max - self.thresh_min) + self.thresh_min

        data['threshold_map'] = canvas
        data['threshold_mask'] = mask
        return data

    def draw_border_map(self, polygon, canvas, mask):
        polygon = np.array(polygon)
        assert polygon.ndim == 2
        assert polygon.shape[1] == 2

        polygon_shape = Polygon(polygon)
        if polygon_shape.area <= 0:
            return
        distance = polygon_shape.area * (
            1 - np.power(self.shrink_ratio, 2)) / polygon_shape.length
        subject = [tuple(l) for l in polygon]
        padding = pyclipper.PyclipperOffset()
        padding.AddPath(subject, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)

        padded_polygon = np.array(padding.Execute(distance)[0])
        cv2.fillPoly(mask, [padded_polygon.astype(np.int32)], 1.0)

        xmin = padded_polygon[:, 0].min()
        xmax = padded_polygon[:, 0].max()
        ymin = padded_polygon[:, 1].min()
        ymax = padded_polygon[:, 1].max()
        width = xmax - xmin + 1
        height = ymax - ymin + 1

        polygon[:, 0] = polygon[:, 0] - xmin
        polygon[:, 1] = polygon[:, 1] - ymin

        xs = np.broadcast_to(
            np.linspace(
                0, width - 1, num=width).reshape(1, width), (height, width))
        ys = np.broadcast_to(
            np.linspace(
                0, height - 1, num=height).reshape(height, 1), (height, width))

        distance_map = np.zeros(
            (polygon.shape[0], height, width), dtype=np.float32)
        for i in range(polygon.shape[0]):
            j = (i + 1) % polygon.shape[0]
            absolute_distance = self._distance(xs, ys, polygon[i], polygon[j])
            distance_map[i] = np.clip(absolute_distance / distance, 0, 1)
        distance_map = distance_map.min(axis=0)

        xmin_valid = min(max(0, xmin), canvas.shape[1] - 1)
        xmax_valid = min(max(0, xmax), canvas.shape[1] - 1)
        ymin_valid = min(max(0, ymin), canvas.shape[0] - 1)
        ymax_valid = min(max(0, ymax), canvas.shape[0] - 1)
        canvas[ymin_valid:ymax_valid + 1, xmin_valid:xmax_valid + 1] = np.fmax(
            1 - distance_map[ymin_valid - ymin:ymax_valid - ymax + height,
                             xmin_valid - xmin:xmax_valid - xmax + width],
            canvas[ymin_valid:ymax_valid + 1, xmin_valid:xmax_valid + 1])

    def _distance(self, xs, ys, point_1, point_2):
        '''
        compute the distance from point to a line
        ys: coordinates in the first axis
        xs: coordinates in the second axis
        point_1, point_2: (x, y), the end of the line
        '''
        height, width = xs.shape[:2]
        square_distance_1 = np.square(xs - point_1[0]) + np.square(ys - point_1[
            1])
        square_distance_2 = np.square(xs - point_2[0]) + np.square(ys - point_2[
            1])
        square_distance = np.square(point_1[0] - point_2[0]) + np.square(
            point_1[1] - point_2[1])

        cosin = (square_distance - square_distance_1 - square_distance_2) / (
            2 * np.sqrt(square_distance_1 * square_distance_2))
        square_sin = 1 - np.square(cosin)
        square_sin = np.nan_to_num(square_sin)
        result = np.sqrt(square_distance_1 * square_distance_2 * square_sin /
                         square_distance)

        result[cosin <
               0] = np.sqrt(np.fmin(square_distance_1, square_distance_2))[cosin
                                                                           < 0]
        # self.extend_line(point_1, point_2, result)
        return result

    def extend_line(self, point_1, point_2, result, shrink_ratio):
        ex_point_1 = (int(
            round(point_1[0] + (point_1[0] - point_2[0]) * (1 + shrink_ratio))),
                      int(
                          round(point_1[1] + (point_1[1] - point_2[1]) * (
                              1 + shrink_ratio))))
        cv2.line(
            result,
            tuple(ex_point_1),
            tuple(point_1),
            4096.0,
            1,
            lineType=cv2.LINE_AA,
            shift=0)
        ex_point_2 = (int(
            round(point_2[0] + (point_2[0] - point_1[0]) * (1 + shrink_ratio))),
                      int(
                          round(point_2[1] + (point_2[1] - point_1[1]) * (
                              1 + shrink_ratio))))
        cv2.line(
            result,
            tuple(ex_point_2),
            tuple(point_2),
            4096.0,
            1,
            lineType=cv2.LINE_AA,
            shift=0)
        return ex_point_1, ex_point_2

class MakeShrinkMap(object):
    r'''
    Making binary mask from detection data with ICDAR format.
    Typically following the process of class `MakeICDARData`.

    adopted from https://github.com/PaddlePaddle/PaddleOCR/blob/HEAD/ppocr/data/imaug/make_shrink_map.py
    '''

    def __init__(self, min_text_size=8, shrink_ratio=0.4, **kwargs):
        self.min_text_size = min_text_size
        self.shrink_ratio = shrink_ratio

    def __call__(self, data):
        image = data['image']
        text_polys = data['polys']
        ignore_tags = data['ignore_tags']

        h, w = image.shape[:2]
        text_polys, ignore_tags = self.validate_polygons(text_polys,
                                                         ignore_tags, h, w)
        gt = np.zeros((h, w), dtype=np.float32)
        mask = np.ones((h, w), dtype=np.float32)
        for i in range(len(text_polys)):
            polygon = text_polys[i]
            height = max(polygon[:, 1]) - min(polygon[:, 1])
            width = max(polygon[:, 0]) - min(polygon[:, 0])
            if ignore_tags[i] or min(height, width) < self.min_text_size:
                cv2.fillPoly(mask,
                             polygon.astype(np.int32)[np.newaxis, :, :], 0)
                ignore_tags[i] = True
            else:
                polygon_shape = Polygon(polygon)
                subject = [tuple(l) for l in polygon]
                padding = pyclipper.PyclipperOffset()
                padding.AddPath(subject, pyclipper.JT_ROUND,
                                pyclipper.ET_CLOSEDPOLYGON)
                shrinked = []

                # Increase the shrink ratio every time we get multiple polygon returned back
                possible_ratios = np.arange(self.shrink_ratio, 1,
                                            self.shrink_ratio)
                np.append(possible_ratios, 1)
                # print(possible_ratios)
                for ratio in possible_ratios:
                    # print(f"Change shrink ratio to {ratio}")
                    distance = polygon_shape.area * (
                        1 - np.power(ratio, 2)) / polygon_shape.length
                    shrinked = padding.Execute(-distance)
                    if len(shrinked) == 1:
                        break

                if shrinked == []:
                    cv2.fillPoly(mask,
                                 polygon.astype(np.int32)[np.newaxis, :, :], 0)
                    ignore_tags[i] = True
                    continue

                for each_shirnk in shrinked:
                    shirnk = np.array(each_shirnk).reshape(-1, 2)
                    cv2.fillPoly(gt, [shirnk.astype(np.int32)], 1)

        data['shrink_map'] = gt
        data['shrink_mask'] = mask
        return data

    def validate_polygons(self, polygons, ignore_tags, h, w):
        '''
        polygons (numpy.array, required): of shape (num_instances, num_points, 2)
        '''
        if len(polygons) == 0:
            return polygons, ignore_tags
        assert len(polygons) == len(ignore_tags)
        for polygon in polygons:
            polygon[:, 0] = np.clip(polygon[:, 0], 0, w - 1)
            polygon[:, 1] = np.clip(polygon[:, 1], 0, h - 1)

        for i in range(len(polygons)):
            area = self.polygon_area(polygons[i])
            if abs(area) < 1:
                ignore_tags[i] = True
            if area > 0:
                polygons[i] = polygons[i][::-1, :]
        return polygons, ignore_tags

    def polygon_area(self, polygon):
        """
        compute polygon area
        """
        area = 0
        q = polygon[-1]
        for p in polygon:
            area += p[0] * q[1] - p[1] * q[0]
            q = p
        return area / 2.0

class DetLabelEncode(object):
    def __init__(self, **kwargs):
        pass

    def order_points_clockwise(self, pts):
        rect = np.zeros((4, 2), dtype="float32")
        s = pts.sum(axis=1)
        rect[0] = pts[np.argmin(s)]
        rect[2] = pts[np.argmax(s)]
        tmp = np.delete(pts, (np.argmin(s), np.argmax(s)), axis=0)
        diff = np.diff(np.array(tmp), axis=1)
        rect[1] = tmp[np.argmin(diff)]
        rect[3] = tmp[np.argmax(diff)]
        return rect

    def expand_points_num(self, boxes):
        max_points_num = 0
        for box in boxes:
            if len(box) > max_points_num:
                max_points_num = len(box)
        ex_boxes = []
        for box in boxes:
            ex_box = box + [box[-1]] * (max_points_num - len(box))
            ex_boxes.append(ex_box)
        return ex_boxes

    def __call__(self, data):
        label = data['label']
        label = json.loads(label)
        nBox = len(label)
        boxes, txts, txt_tags = [], [], []
        for bno in range(0, nBox):
            box = label[bno]['points']
            txt = label[bno]['transcription']
            boxes.append(box)
            txts.append(txt)
            if txt in ['*', '###']:
                txt_tags.append(True)
            else:
                txt_tags.append(False)
        if len(boxes) == 0:
            return None
        boxes = self.expand_points_num(boxes)
        boxes = np.array(boxes, dtype=np.float32)
        txt_tags = np.array(txt_tags, dtype=np.bool)

        data['polys'] = boxes
        data['texts'] = txts
        data['ignore_tags'] = txt_tags
        return data

class RecLabelEncode(object):
    """ Convert between text-label and text-index """

    def __init__(self,
                 max_text_length,
                 character_dict_path=None,
                 use_space_char=False,
                 lower=False):

        self.max_text_len = max_text_length
        self.beg_str = "sos"
        self.end_str = "eos"
        self.lower = lower

        if character_dict_path is None:
            logger = get_logger()
            logger.warning(
                "The character_dict_path is None, model can only recognize number and lower letters"
            )
            self.character_str = "0123456789abcdefghijklmnopqrstuvwxyz"
            dict_character = list(self.character_str)
            self.lower = True
        else:
            self.character_str = []
            with open(character_dict_path, "rb") as fin:
                lines = fin.readlines()
                for line in lines:
                    line = line.decode('utf-8').strip("\n").strip("\r\n")
                    self.character_str.append(line)
            if use_space_char:
                self.character_str.append(" ")
            dict_character = list(self.character_str)
        dict_character = self.add_special_char(dict_character)
        self.dict = {}
        for i, char in enumerate(dict_character):
            self.dict[char] = i
        self.character = dict_character

    def add_special_char(self, dict_character):
        return dict_character

    def encode(self, text):
        """convert text-label into text-index.
        input:
            text: text labels of each image. [batch_size]

        output:
            text: concatenated text index for CTCLoss.
                    [sum(text_lengths)] = [text_index_0 + text_index_1 + ... + text_index_(n - 1)]
            length: length of each text. [batch_size]
        """
        if len(text) == 0 or len(text) > self.max_text_len:
            return None
        if self.lower:
            text = text.lower()
        text_list = []
        for char in text:
            if char not in self.dict:
                # logger = get_logger()
                # logger.warning('{} is not in dict'.format(char))
                continue
            text_list.append(self.dict[char])
        if len(text_list) == 0:
            return None
        return text_list

class CTCLabelEncode(RecLabelEncode):
    """ Convert between text-label and text-index """

    def __init__(self,
                 max_text_length,
                 character_dict_path=None,
                 use_space_char=False,
                 **kwargs):
        super().__init__(
            max_text_length, character_dict_path, use_space_char)

    def __call__(self, data):
        text = data['label']
        text = self.encode(text)
        if text is None:
            return None
        data['length'] = np.array(len(text))
        text = text + [0] * (self.max_text_len - len(text))
        data['label'] = np.array(text)

        label = [0] * len(self.character)
        for x in text:
            label[x] += 1
        data['label_ace'] = np.array(label)
        return data

    def add_special_char(self, dict_character):
        dict_character = ['blank'] + dict_character
        return dict_character 

class DecodeImage(object):
    '''
    img_mode: 'BGR', 'RGB', 'GRAY'
    '''
    def __init__(self, img_mode='BGR', channel_first=False, to_float32=False, **kwargs):
        self.img_mode = img_mode 
        self.to_float32 = to_float32
        self.channel_first = channel_first

    def __call__(self, data):
        # TODO: use more efficient image loader, read binary, then decode?
        # TODO: why float32 in modelzoo. uint8 is more efficient
        
        img = cv2.imread(data['img_path'], cv2.IMREAD_COLOR if self.img_mode != 'GRAY' else cv2.IMREAD_GRAYSCALE)
        
        if self.img_mode == 'RGB':
            img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)

        if self.channel_first:
            img = img.transpose((2, 0, 1))

        if self.to_float32:
            img = img.astype('float32')
        data['image'] = img
        #data['ori_image'] = img.copy() 
        return data

class NormalizeImage(object):
    """ normalize image, substract mean, divide std
    input image: by default, np.uint8, [0, 255], HWC format. 
    return image: float32 numpy array 
    """

    def __init__(self, mean=IMAGENET_DEFAULT_MEAN, std=IMAGENET_DEFAULT_STD, is_hwc=True, bgr_to_rgb=False, rgb_to_bgr=False, **kwargs):
        # By default, imagnet MEAN and STD is in RGB order. inverse if input image is in BGR mode
        self._channel_conversion = False
        if bgr_to_rgb or rgb_to_bgr:
            self._channel_conversion = True

        # TODO: detect hwc or chw automatically
        shape = (3, 1, 1) if not is_hwc else (1, 1, 3)
        self.mean = np.array(mean).reshape(shape).astype('float32')
        self.std = np.array(std).reshape(shape).astype('float32')
        self.is_hwc = is_hwc

    def __call__(self, data):
        img = data['image']
        if isinstance(img, Image.Image):
            img = np.array(img)
        assert isinstance(img, np.ndarray), "invalid input 'img' in NormalizeImage"

        if self._channel_conversion:
            if self.is_hwc:
                img = img[..., [2,1,0]]
            else:
                img = img[[2,1,0], ...]

        data['image'] = (
            img.astype('float32') - self.mean) / self.std
        return data

class ToCHWImage(object):
    """ convert hwc image to chw image
    """

    def __init__(self, **kwargs):
        pass

    def __call__(self, data):
        img = data['image']
        if isinstance(img, Image.Image):
            img = np.array(img)
        data['image'] = img.transpose((2, 0, 1))
        return data

class FilterKeys(object):
    ''' 
    Args: 
        output_keys (list): the keys in data dict that are expected to output for dataloader
    
    Call:
        input: data dict
        output: data tuple corresponding to the `output_keys` 
    '''
    def __init__(self, output_keys: List, **kwargs):
        self.output_keys = output_keys

    def __call__(self, data):
        # TOOD: add assert for inexisted keys
        out = []
        for k in self.output_keys:
            assert k in data, f'key {k} does not exists in data, availabe keys are {data.keys()}'
            out.append(data[k])
        #data_tuple = tuple(data[k] for k in self.output_keys if )

        return tuple(out)

# TODO: use class with __call__, to perform transformation 
def create_transforms(transform_pipeline, global_config=None):
    """
    Create a squence of callable transforms.

    Args:
        transform_pipeline (List): list of callable instances or dicts where each key is a transformation class name, and its value are the args. 
            e.g. [{'DecodeImage': {'img_mode': 'BGR', 'channel_first': False}}]
                 [DecodeImage(img_mode='BGR')]
                    
    Returns:
        list of data transformation functions
    """
    assert isinstance(transform_pipeline, list), (f'transform_pipeline config should be a list, but {type(transform_pipeline)} detected')

    transforms = []
    for transform_config in transform_pipeline:
        if isinstance(transform_config, dict): 
            assert len(transform_config) == 1, "yaml format error in transforms"
            trans_name = list(transform_config.keys())[0]
            param = {} if transform_config[trans_name] is None else transform_config[trans_name]
            #  TODO: not each transform needs global config
            if global_config is not None:
                param.update(global_config)
            # TODO: assert undefined transform class

            print(trans_name, param)
            transform = eval(trans_name)(**param)
            transforms.append(transform)
        elif callable(transform_config):
            transforms.append(transform_config)
        else:
            raise TypeError('transform_config must be a dict or a callable instance')
        print(global_config)
    return transforms

def run_transforms(data, transforms=None):
    """ transform """
    if transforms is None:
        transforms = []
    for transform in transforms:
        data = transform(data)
        if data is None:
            return None
    return data


def transforms_dbnet_icdar15(phase='train'):
    '''
    Get pre-defined transform config for dbnet on icdar15 dataset.
    Args:
        phase: train, eval, infer 
    Returns:
        list of dict for data transformation pipeline, which can be convert to functions by 'create_transforms' 
    '''
    if phase == 'train':
        pipeline = [
                    {'DecodeImage': {
                        'img_mode': 'BGR', 
                        'to_float32': False}},
                    {'DetLabelEncode': None}, 
                    {'MZResizeByGrid': {'denominator': 32, 'transform_polys': True}}, # prev in modelzoo, it doesn't transform polys
                    {'MZRandomScaleByShortSide': {'short_side': 736}},
                    {'IaaAugment': {'augmenter_args':
                        [{'type': 'Affine',
                            'args': {'rotate': [-10, 10]}
                        }, 
                        {'type': 'Fliplr',
                            'args': {
                                'p': 0.5}
                        },
                        ]}
                    },
                    {'MZRandomCropData': 
                            {'max_tries':100, 
                            'min_crop_side_ratio': 0.1,
                            'crop_size': (640, 640)}},
                    {'MZResizeByGrid': {'denominator': 32, 'transform_polys': True}},
                    #{'MakeShrinkMap': 
                    {'MZMakeSegDetectionData': 
                        {'min_text_size': 8, 'shrink_ratio': 0.4}},
                    #{'MakeBorderMap': 
                    {'MZMakeBorderMap': 
                        {'shrink_ratio': 0.4, 'thresh_min': 0.3, 'thresh_max': 0.7,
                    }},
                    {'MZRandomColorAdjust': {'brightness': 32.0 / 255, 'saturation':0.5, 'to_numpy':True}},
                    #{'MZIrregularNormToCHW': None},
                    {'NormalizeImage': {
                        'bgr_to_rgb': True,
                        'is_hwc': True,
                        'mean' : [123.675, 116.28, 103.53],
                        'std' : [58.395, 57.12, 57.375],
                        }
                    },
                    {'ToCHWImage': None}
                    ]

    elif phase=='eval':
        pipeline = [
                    {'DecodeImage': {'img_mode': 'BGR', 'to_float32': False}},
                    {'DetLabelEncode': None}, 
                    {'MZResizeByGrid': {'denominator': 32, 'transform_polys': True}}, # prev in modelzoo, it doesn't transform polys

                    {'MZScalePad': {'eval_size': [736, 1280]}},                     
                    {'NormalizeImage': {
                        'bgr_to_rgb': True,
                        'is_hwc': True,
                        'mean' : [123.675, 116.28, 103.53],
                        'std' : [58.395, 57.12, 57.375],
                        }
                    },
                    {'ToCHWImage': None}

                    ]
    else:
        pipeline = [
                    {'DecodeImage': {'img_mode': 'BGR', 'to_float32': False}},
                    {'MZResizeByGrid': {'denominator': 32, 'transform_polys': True}}, 
                    {'MZScalePad': {'eval_size': [736, 1280]}},                     
                    {'NormalizeImage': {
                        'bgr_to_rgb': True,
                        'is_hwc': True,
                        'mean' : [123.675, 116.28, 103.53],
                        'std' : [58.395, 57.12, 57.375],
                        }
                    },
                    {'ToCHWImage': None}
                    ]
    return 
