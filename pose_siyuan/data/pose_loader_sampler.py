import torch
import torch.utils.data as data
import numpy as np
import shutil
import time
import random
import os
import math
import json
from PIL import Image
import cv2
import Mytransforms


def sample_weight_by_invisible_joints(json_file):
    fp = open(json_file)
    data = json.load(fp)
    num_visible_joints = 0
    num_other_jpints = 0

    human_list = data['human_list']
    for human in human_list:
        kpts = read_kpt(human['human_keypoints'])
        for kpt in kpts:
            if kpt[2] < 2:
                num_visible_joints = num_visible_joints + 1
            else:
                num_other_jpints = num_other_jpints + 1


    sample_weight = num_other_jpints * 1.0 / (num_other_jpints + num_visible_joints)

    return sample_weight


def read_kpt(kpt_list):
    kpt = []
    for kpt_dict in kpt_list:
        kpt_part = [kpt_dict['x'], kpt_dict['y'], kpt_dict['is_visible']]
        kpt.append(kpt_part)
    return kpt



def read_json(json_file, crop_size = 368):
    fp = open(json_file)
    data = json.load(fp)
    kpts = []
    centers = []
    scales = []

    human_list = data['human_list']

    for human in human_list:
        kpt = read_kpt(human['human_keypoints'])

        rect = human['human_rect_origin']
        center_x = rect['x'] + rect['w'] / 2.0
        center_y = rect['y'] + rect['h'] / 2.0
        center = [center_x, center_y]
        scale = rect['h'] * 1.0 / crop_size
        if scale< 0.05:
            scale = 1.0

        kpts.append(kpt)
        centers.append(center)
        scales.append(scale)

    return kpts, centers, scales


class GenerateWeightMask():
    def __init__(self,rad,weight):
        self.rad = rad
        self.g = weight*np.ones((2*rad+1,2*rad+1))

    def __call__(self, keypoints,output_res,num_parts,num_pafs,stride):
        mask = np.ones(shape=(output_res, output_res, num_parts+2*num_pafs + 1), dtype=np.float32)
        rad = self.rad
        for p in keypoints:
            for idx, pt in enumerate(p):
                if pt[2]==2:
                    x, y = int(pt[0]/float(stride)), int(pt[1]/float(stride))
                    if x<0 or y<0 or x>=output_res or y>=output_res:
                        #print('not in', x, y)
                        continue
                    ul = int(x - rad), int(y - rad)
                    br = int(x + rad+1), int(y + rad+1)

                    c,d = max(0, -ul[0]), min(br[0], output_res) - ul[0]
                    a,b = max(0, -ul[1]), min(br[1], output_res) - ul[1]

                    cc,dd = max(0, ul[0]), min(br[0], output_res)
                    aa,bb = max(0, ul[1]), min(br[1], output_res)

                    mask[aa:bb,cc:dd,idx+1] =  self.g[a:b,c:d]
                    mask[aa:bb, cc:dd, 0] = self.g[a:b, c:d]
        return mask



def generate_heatmap(heatmap, kpt, stride, sigma):

    height, width, num_point = heatmap.shape
    start = stride / 2.0 - 0.5

    num = len(kpt)
    length = len(kpt[0])
    for i in range(num):
        for j in range(length):
            if kpt[i][j][2] > 1:
                continue
            x = kpt[i][j][0]
            y = kpt[i][j][1]
            for h in range(height):
                for w in range(width):
                    xx = start + w * stride
                    yy = start + h * stride
                    dis = ((xx - x) * (xx - x) + (yy - y) * (yy - y)) / 2.0 / sigma / sigma
                    if dis > 4.6052:
                        continue
                    heatmap[h][w][j + 1] += math.exp(-dis)
                    if heatmap[h][w][j + 1] > 1:
                        heatmap[h][w][j + 1] = 1

    return heatmap

def generate_vector(vector, cnt, kpts, vec_pair, stride, theta):

    height, width, channel = cnt.shape
    length = len(kpts)

    for j in range(length):
        for i in range(channel):
            a = vec_pair[0][i]
            b = vec_pair[1][i]
            if kpts[j][a][2] > 1 or kpts[j][b][2] > 1:
                continue
            ax = kpts[j][a][0] * 1.0 / stride
            ay = kpts[j][a][1] * 1.0 / stride
            bx = kpts[j][b][0] * 1.0 / stride
            by = kpts[j][b][1] * 1.0 / stride

            bax = bx - ax
            bay = by - ay
            norm_ba = math.sqrt(1.0 * bax * bax + bay * bay) + 1e-9 # to aviod two points have same position.
            bax /= norm_ba
            bay /= norm_ba

            min_w = max(int(round(min(ax, bx) - theta)), 0)
            max_w = min(int(round(max(ax, bx) + theta)), width)
            min_h = max(int(round(min(ay, by) - theta)), 0)
            max_h = min(int(round(max(ay, by) + theta)), height)

            for h in range(min_h, max_h):
                for w in range(min_w, max_w):
                    px = w - ax
                    py = h - ay

                    dis = abs(bay * px - bax * py)
                    if dis <= theta:
                        vector[h][w][2 * i] = (vector[h][w][2 * i] * cnt[h][w][i] + bax) / (cnt[h][w][i] + 1)
                        vector[h][w][2 * i + 1] = (vector[h][w][2 * i + 1] * cnt[h][w][i] + bay) / (cnt[h][w][i] + 1)
                        cnt[h][w][i] += 1

    return vector






class PoseData(data.Dataset):
    def __init__(self, args, transformer = None,isMask=False):
        self.args = args
        self.data_list = self.read(self.args['desc_file_path'])
        self.crop_size = int(self.args['crop_size'])
        self.rotate = int(self.args['rotate'])
        self.stride = int(self.args['stride'])
        self.transformer = transformer
        self.vec_pair = [[0, 0, 0, 1, 1, 2, 4, 3, 5, 2, 3, 8, 9, 10,11],
                         [1, 2, 3, 2, 3, 4, 6, 5, 7, 8, 9, 10, 11, 12,13]]  # different from openpose
        self.theta = 1.0
        self.sigma = 7.0
        self.generateWeightMask = GenerateWeightMask(2, 2)
        self.isMask = isMask

    def __getitem__(self, index):

        while(True):
            img_path, json_path = self.data_list[index]
            img = cv2.imread(img_path)
            if img is not None:

                img = np.array(img, dtype=np.float32)
                break
            else:
                index = np.random.randint(0,10)


        mask = np.ones((img.shape[0], img.shape[1]))
        kpt, center, scale = read_json(json_path, crop_size=self.crop_size)
        if self.transformer is not None:
            img, mask, kpt, center = self.transformer(img, mask, kpt, center, scale)
        height, width, _ = img.shape

        mask = cv2.resize(mask, (width / self.stride, height / self.stride)).reshape(
            (height / self.stride, width / self.stride, 1))

        heatmap = np.zeros((height / self.stride, width / self.stride, len(kpt[0]) + 1), dtype=np.float32)
        heatmap = generate_heatmap(heatmap, kpt, self.stride, self.sigma)
        heatmap[:, :, 0] = 1.0 - np.max(heatmap[:, :, 1:], axis=2)  # for background
        if self.isMask:
            mask = self.generateWeightMask(kpt, height / self.stride, len(kpt[0]), len(self.vec_pair[0]), self.stride)

        vecmap = np.zeros((height / self.stride, width / self.stride, len(self.vec_pair[0]) * 2), dtype=np.float32)
        cnt = np.zeros((height / self.stride, width / self.stride, len(self.vec_pair[0])), dtype=np.int32)

        vecmap = generate_vector(vecmap, cnt, kpt, self.vec_pair, self.stride, self.theta)

        img = Mytransforms.normalize(Mytransforms.to_tensor(img), [128.0, 128.0, 128.0],
                                     [256.0, 256.0, 256.0])  # mean, std
        mask = Mytransforms.to_tensor(mask)
        heatmap = Mytransforms.to_tensor(heatmap)
        vecmap = Mytransforms.to_tensor(vecmap)
        #print img.shape

        if self.isMask:
            return img, torch.cat([heatmap, vecmap], 0),mask
        else:
            return img, torch.cat([heatmap, vecmap], 0)



    def __len__(self):
        return len(self.data_list)

    def get_sample_weight_by_invisible_joints(self):
        sample_weights = []
        for data_list in self.data_list:
            img_path, json_path = data_list
            sample_weight = sample_weight_by_invisible_joints(json_path)
            sample_weights.append(sample_weight)
            print len(sample_weights)

        return sample_weights

    def writeSampleWeight(self):
        f = open('SampleWeight.txt','w')
        sample_weights = []
        for id,data_list in enumerate(self.data_list):
            img_path, json_path = data_list
            sample_weight = sample_weight_by_invisible_joints(json_path)
            sample_weights.append(sample_weight)
            f.write(str(sample_weight)+'\n')
            if (id+1)%1000==0:
                f.flush()
            print(len(sample_weights))
        f.close()
        return sample_weights





    def read(self, desc_file_path):
        import codecs

        result = []
        data_root = self.args.get("data_root", None)

        with codecs.open(desc_file_path, 'r', 'utf-8') as txt:
            for line in txt:
                line = line.strip()
                input_path, target_path = line.encode('utf-8').split(' ')

                if data_root:
                    input_path  = os.path.join(data_root, input_path)
                    target_path = os.path.join(data_root, target_path)
                result.append((input_path, target_path))

            print "read from {} total {} ".format(desc_file_path, len(result))

        return result



if __name__ == '__main__':
    args = dict()
    args['desc_file_path'] = '/root/qxc/code/pose_estimation/squid/data/AI/txt/ai_train.txt'
    args['data_root'] = '/root/data-collection/AI_Challenger_keypoint/ai_challenger_keypoint_train_20170902'
    args['crop_size'] = '368'
    args['rotate'] = 40
    args['stride'] = 8
    transform = Mytransforms.Compose([
                #Mytransforms.RandomHorizontalFlip(),
                Mytransforms.RandomResized(),
                Mytransforms.RandomRotate(40),
                Mytransforms.RandomCrop(368),
                #Mytransforms.RandomHorizontalFlip(),
            ])
    p = PoseData(args, transform)

    import matplotlib
    import matplotlib.pyplot as plt

    for i, data in enumerate(p):
        if i  <100:
            continue
        img, (heatmap, vecmap) = data
        print i, img.shape, heatmap.shape, vecmap.shape

        imgs = img.numpy()
        heats = heatmap.numpy()
        vectors = vecmap.numpy()


        for i in range(1):
            img = imgs[:, :, :]
            img = img.transpose(1, 2, 0)
            img *= 128
            img += 128
            img /= 255
            plt.imshow(img)
            plt.show()
            #plt.close()



            heatmaps = heats[:, :, :]
            heatmaps = heatmaps.transpose(1, 2, 0)
            heatmaps = cv2.resize(heatmaps, (368, 368))
            for j in range(0, 1):
                heatmap = heatmaps[:, :, j]
                heatmap = heatmap.reshape((368, 368, 1))
                heatmap *= 255
                heatmap = cv2.applyColorMap(heatmap, cv2.COLORMAP_JET)
                # heatmap = heatmap.reshape((368,368,1))
                heatmap /= 255
                # result = heatmap * 0.4 + img * 0.5
                print j
                plt.imshow(img)
                plt.imshow(heatmap, alpha=0.5)
                plt.show()
                plt.close()

            vecs = vectors[:, :, :]
            vecs = vecs.transpose(1, 2, 0)
            vecs = cv2.resize(vecs, (368, 368))
            for j in range(0, 2, 2):
                vec = np.abs(vecs[:, :, j])
                vec += np.abs(vecs[:, :, j + 1])
                vec[vec > 1] = 1
                vec = vec.reshape((368, 368, 1))
                # vec[vec > 0] = 1
                vec *= 255
                vec = cv2.applyColorMap(vec, cv2.COLORMAP_JET)
                vec = vec.reshape((368, 368))
                vec /= 255
                print j
                plt.imshow(img)
                # result = vec * 0.4 + img * 0.5
                plt.imshow(vec, alpha=0.5)
                plt.show()
                plt.close()

    print 'Done'


