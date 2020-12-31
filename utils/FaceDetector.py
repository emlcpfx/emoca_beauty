from abc import abstractmethod, ABC
import numpy as np
import torch
from memory_profiler import profile


class FaceDetector(ABC):

    @abstractmethod
    def run(self, image):
        raise NotImplementedError()

    def __call__(self, *args, **kwargs):
        self.run(*args, **kwargs)


class FAN(FaceDetector):

    def __init__(self, device = 'cuda', threshold=0.5):
        import face_alignment
        self.face_detector = 'sfd'
        self.face_detector_kwargs = {
            "filter_threshold": threshold
        }
        self.model = face_alignment.FaceAlignment(face_alignment.LandmarksType._2D,
                                                  device=str(device),
                                                  flip_input=False,
                                                  face_detector=self.face_detector,
                                                  face_detector_kwargs=self.face_detector_kwargs)

    @profile
    def run(self, image):
        '''
        image: 0-255, uint8, rgb, [h, w, 3]
        return: detected box list
        '''
        out = self.model.get_landmarks(image)
        if out is None:
            del out
            return [], 'kpt68'
        else:
            boxes = []
            for i in range(len(out)):
                kpt = out[i].squeeze()
                left = np.min(kpt[:, 0])
                right = np.max(kpt[:, 0])
                top = np.min(kpt[:, 1])
                bottom = np.max(kpt[:, 1])
                bbox = [left, top, right, bottom]
                boxes += [bbox]
            del out # attempt to prevent memory leaks
            return boxes, 'kpt68'


class MTCNN(FaceDetector):

    def __init__(self, device = 'cuda'):
        '''
        https://github.com/timesler/facenet-pytorch/blob/master/examples/infer.ipynb
        '''
        from facenet_pytorch import MTCNN as mtcnn
        self.device = device
        self.model = mtcnn(keep_all=True, device=device)

    def run(self, input):
        '''
        image: 0-255, uint8, rgb, [h, w, 3]
        return: detected box
        '''
        out = self.model.detect(input[None,...])
        if out[0][0] is None:
            return [], 'bbox'
        else:
            bboxes = []
            for i in range(out.shape[0]):
                bbox = out[0][0].squeeze()
                bboxes += [bbox]
            return bboxes, 'bbox'

