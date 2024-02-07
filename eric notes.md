8/11/23
Updated detect.py with code to improve speed in SFD
C:\anaconda3\envs\work38\lib\site-packages\face_alignment\detection\sfd\detect.py

Code Here: https://github.com/1adrianb/face-alignment/pull/347

v1 of the code speeds up from 3:11 to 2:50, about 10%

If you switch from sfd to blazeface, it goes down to 2:32.
    In emoca/gdl/utils/FaceDetector.py:

        class FAN(FaceDetector):

        def __init__(self, device = 'cuda', threshold=0.5):
            import face_alignment
            self.face_detector = 'blazeface'

