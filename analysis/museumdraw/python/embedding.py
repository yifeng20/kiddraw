from __future__ import division
from __future__ import print_function
from __future__ import absolute_import

import copy
import numpy as np

import torch
import torchvision.models as models
import torch.nn as nn
import torchvision.transforms as transforms
import torch.nn.functional as F
from torch.autograd import Variable

from PIL import Image

use_cuda = torch.cuda.is_available()

class VGG19Embeddings(nn.Module):
    """Splits vgg19 into separate sections so that we can get
    feature embeddings from each section.

    :param vgg19: traditional vgg19 model
    """
    def __init__(self, vgg19, layer_index=-1):
        super(VGG19Embeddings, self).__init__()
        self.conv1 = nn.Sequential(*(list(vgg19.features.children())[slice(0, 5)]))
        self.conv2 = nn.Sequential(*(list(vgg19.features.children())[slice(5, 10)]))
        self.conv3 = nn.Sequential(*(list(vgg19.features.children())[slice(10, 19)]))
        self.conv4 = nn.Sequential(*(list(vgg19.features.children())[slice(19, 28)]))
        self.conv5 = nn.Sequential(*(list(vgg19.features.children())[slice(28, 37)]))
        self.linear1 = nn.Sequential(*(list(vgg19.classifier.children())[slice(0, 2)]))
        self.linear2 = nn.Sequential(*(list(vgg19.classifier.children())[slice(3, 5)]))
        self.linear3 = nn.Sequential(list(vgg19.classifier.children())[-1])
        layer_index = int(float(layer_index)) # bll 
        assert layer_index >= -1 and layer_index < 8
        self.layer_index = layer_index

    def _flatten(self, x):
        return x.view(x.size(0), -1)

    def forward(self, x):
        # build in this ugly way so we don't have to evaluate things we don't need to.
        x_conv1 = self.conv1(x)
        if self.layer_index == 0:
            return [self._flatten(x_conv1)]
        x_conv2 = self.conv2(x_conv1)
        if self.layer_index == 1:
            return [self._flatten(x_conv2)]
        x_conv3 = self.conv3(x_conv2)
        if self.layer_index == 2:
            return [self._flatten(x_conv3)]
        x_conv4 = self.conv4(x_conv3)
        if self.layer_index == 3:
            return [self._flatten(x_conv4)]
        x_conv5 = self.conv5(x_conv4)
        x_conv5_flat = self._flatten(x_conv5)
        if self.layer_index == 4:
            return [x_conv5_flat]
        x_linear1 = self.linear1(x_conv5_flat)
        if self.layer_index == 5:
            return [x_linear1]
        x_linear2 = self.linear2(x_linear1)
        if self.layer_index == 6:
            return [x_linear2]
        x_linear3 = self.linear3(x_linear2)
        if self.layer_index == 7:
            return [x_linear3]
        return [self._flatten(x_conv1), self._flatten(x_conv2),
                self._flatten(x_conv3), self._flatten(x_conv4),
                self._flatten(x_conv5), x_linear1, x_linear2, x_linear3]
        
class FeatureExtractor():
    
    def __init__(self,paths,layer=6, use_cuda=True, imsize=224, batch_size=64, cuda_device=3, cohort='kid'):
        self.layer = layer
        self.paths = paths
        self.num_sketches = len(self.paths)
        self.use_cuda = use_cuda
        self.imsize = imsize
        self.padding = 10
        self.batch_size = batch_size       
        self.cuda_device = cuda_device
        self.cohort = cohort ## 'kid' if analyzing kids' drawings; 'adult' if analyzing adults' drawings

    def extract_feature_matrix(self):
        
        def RGBA2RGB(image, color=(255, 255, 255)):
            """Alpha composite an RGBA Image with a specified color.

            Simpler, faster version than the solutions above.

            Source: http://stackoverflow.com/a/9459208/284318

            Keyword Arguments:
            image -- PIL RGBA Image object
            color -- Tuple r, g, b (default 255, 255, 255)

            """
            image.load()  # needed for split()
            background = Image.new('RGB', image.size, color)
            background.paste(image, mask=image.split()[3])  # 3 is the alpha channel
            return background

        def load_image(path, imsize=224, padding=self.padding, volatile=True, use_cuda=False):
            im = Image.open(path)
            im = RGBA2RGB(im)
            
            # crop to sketch only (eliminate white space)
            arr = np.asarray(im)
            w,h,d = np.where(arr<255) # where the image is not white
            xlb = min(h)
            xub = max(h)
            ylb = min(w)
            yub = max(w)
            im = im.crop((xlb, ylb, xub, yub))            

            loader = transforms.Compose([
                transforms.Scale(imsize),
                transforms.Pad(padding),
                transforms.ToTensor()])

            im = Variable(loader(im), volatile=volatile)
            im = im.unsqueeze(0)
            if use_cuda:
                im = im.cuda(self.cuda_device)
            return im        
        
        def load_vgg19(layer_index=self.layer,use_cuda=True,cuda_device=self.cuda_device):
            vgg19 = models.vgg19(pretrained=True).cuda(self.cuda_device)        
            vgg19 = VGG19Embeddings(vgg19,layer_index)
            vgg19.eval()  # freeze dropout

            # freeze each parameter
            for p in vgg19.parameters():
                p.requires_grad = False

            return vgg19  
        
        def get_metadata_from_path(path):
            label = path.split('/')[-2]            
            if self.cohort == 'kid':
                age = path.split('/')[-1].split('_')[2]
                session = path.split('/')[-1].split('.')[0].split('_')[-2] + '_' + path.split('/')[-1].split('.')[0].split('_')[-1]
            elif self.cohort == 'adult':
                age = 'adult'
                session = 'unknown'
            else:
                print('Need to specify a cohort: "kid" or "adult"!')
                age = 'unknown'
                session = 'unknown'
            return label, age, session        

        def generator(paths, imsize=self.imsize, use_cuda=use_cuda):
            for path in paths:
                image = load_image(path)
                label, age, session = get_metadata_from_path(path)
                yield (image, label, age, session)        
                                                
        # define generator
        generator = generator(self.paths,imsize=self.imsize,use_cuda=self.use_cuda)
        
        # initialize sketch and label matrices
        Features = []
        Labels = []
        Ages = []
        Sessions = []
        
        n = 0
        quit = False 
        
        # load appropriate extractor
        extractor = load_vgg19(layer_index=self.layer)        
        
        # generate batches of sketches and labels    
        if generator:
            while True:    
                batch_size = self.batch_size
                sketch_batch = Variable(torch.zeros(batch_size, 3, self.imsize, self.imsize))                
                if use_cuda:
                    sketch_batch = sketch_batch.cuda(self.cuda_device)             
                label_batch = [] 
                age_batch = []
                session_batch = []
                if (n+1)%1==0:
                    print('Batch {}'.format(n + 1))            
                for b in range(batch_size):
                    try:
                        sketch, label, age, session = generator.next()
                        sketch_batch[b] = sketch 
                        label_batch.append(label)
                        age_batch.append(age)
                        session_batch.append(session)
                    except StopIteration:
                        quit = True
                        print('stopped!')
                        break                
                
                n = n + 1       
                if n == self.num_sketches//self.batch_size:
                    sketch_batch = sketch_batch.narrow(0,0,b)
                    label_batch = label_batch[:b + 1] 
                    age_batch = age_batch[:b + 1]   
                    session_batch = session_batch[:b + 1]
                
                # extract features from batch
                sketch_batch = extractor(sketch_batch)
                sketch_batch = sketch_batch[0].cpu().data.numpy()

                if len(Features)==0:
                    Features = sketch_batch
                else:
                    Features = np.vstack((Features,sketch_batch))

                Labels.append(label_batch)
                Ages.append(age_batch)
                Sessions.append(session_batch)

                if n == self.num_sketches//batch_size + 1:
                    break
        Labels = np.array([item for sublist in Labels for item in sublist])
        Ages = np.array([item for sublist in Ages for item in sublist])
        Sessions = np.array([item for sublist in Sessions for item in sublist])
        return Features, Labels, Ages, Sessions
    