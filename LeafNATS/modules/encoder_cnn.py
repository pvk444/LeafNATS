'''
@author Tian Shi
Please contact tshi@vt.edu
'''
import torch
from torch.autograd import Variable

class EncoderCNN(torch.nn.Module):
    
    def __init__(
        self,
        emb_dim,
        list_kernel_size, # 3,4,5
        list_kernel_nums  # 100, 200, 100
    ):
        '''
        Implementation of CNN encoder.
        '''
        super(EncoderCNN, self).__init__()
        
        kSize = re.split(',', list_kernel_size)
        kSize = [int(itm) for itm in kSize]
        kNums = re.split(',', list_kernel_nums)
        kNums = [int(itm) for itm in kNums]
        if len(kSize) != len(kNums):
            print("Size mismatch!")
            
        self.convs1 = torch.nn.ModuleList([
            torch.nn.Conv2d(1, kNums[k], (kSize[k], emb_dim)) 
            for k in range(len(kNums))]).cuda()
        
    def forward(self, input_):
        '''
        input_ embeddings.
        Not finished.
        '''
        h0 = [F.relu(conv(input_)).squeeze(3) for conv in self.convs1]
        h0 = [F.max_pool1d(k, k.size(2)).squeeze(2) for k in h0]
        h0 = torch.cat(h0, 1)
        
        return h0
