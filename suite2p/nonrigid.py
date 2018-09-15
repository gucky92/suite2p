import numpy as np
from numpy import fft
from scipy.ndimage import gaussian_filter
from skimage.transform import warp#, PiecewiseAffineTransform
from scipy.interpolate import interp2d
from suite2p import register
import time
import multiprocessing
from multiprocessing import Pool

eps0 = 1e-5;
sigL = 0.85 # smoothing width for up-sampling kernels, keep it between 0.5 and 1.0...
lpad = 3   # upsample from a square +/- lpad
smoothSigma = 1.15 # smoothing constant
maskSlope   = 2. # slope of taper mask at the edges

def prepare_masks(refImg0, ops):
    # split refImg0 into multiple parts
    cfRefImg1 = []
    maskMul1 = []
    maskOffset1 = []
    nb = len(ops['yblock'])
    print(ops['yblock'])
    print(ops['xblock'])
    for n in range(nb):
        yind = ops['yblock'][n]
        yind = np.arange(yind[0],yind[-1]).astype('int')
        xind = ops['xblock'][n]
        xind = np.arange(xind[0],xind[-1]).astype('int')
        refImg = refImg0[np.ix_(yind,xind)]
        Ly,Lx = refImg.shape
        if n==0:
            cfRefImg1 = np.zeros((nb,1,Ly,Lx),'complex64')
            maskMul1 = np.zeros((nb,1,Ly,Lx),'float32')
            maskOffset1 = np.zeros((nb,1,Ly,Lx),'float32')
        x = np.arange(0, Lx)
        y = np.arange(0, Ly)
        x = np.abs(x - x.mean())
        y = np.abs(y - y.mean())
        xx, yy = np.meshgrid(x, y)
        mY = y.max() - 4.
        mX = x.max() - 4.
        maskY = 1./(1.+np.exp((yy-mY)/maskSlope))
        maskX = 1./(1.+np.exp((xx-mX)/maskSlope))
        maskMul = maskY * maskX
        maskOffset = refImg.mean() * (1. - maskMul);
        hgx = np.exp(-np.square(xx/smoothSigma))
        hgy = np.exp(-np.square(yy/smoothSigma))
        hgg = hgy * hgx
        hgg = hgg/hgg.sum()
        fhg = np.real(fft.fft2(fft.ifftshift(hgg))); # smoothing filter in Fourier domain
        cfRefImg   = np.conj(fft.fft2(refImg));
        absRef     = np.absolute(cfRefImg);
        cfRefImg   = cfRefImg / (eps0 + absRef) * fhg;
        maskMul1[n,0,:,:] = (maskMul.astype('float32'))
        maskOffset1[n,0,:,:] = (maskOffset.astype('float32'))
        cfRefImg1[n,0,:,:] = (cfRefImg.astype('complex64'))
    return maskMul1, maskOffset1, cfRefImg1

def correlation_map(data, refAndMasks):
    maskMul    = refAndMasks[0]
    maskOffset = refAndMasks[1]
    cfRefImg   = refAndMasks[2]
    nb, nimg, Ly, Lx = data.shape
    data = data.astype('float32') * maskMul + maskOffset
    X = fft.fft2(data)
    J = X / (eps0 + np.absolute(X))
    J = J * cfRefImg
    cc = np.real(fft.ifft2(J))
    cc = fft.fftshift(cc, axes=(2,3))
    return cc

def phasecorr_worker(inputs):
    ''' loop through blocks and compute phase correlations'''
    data, refAndMasks, ops = inputs
    maskMul1    = refAndMasks[0]
    maskOffset1 = refAndMasks[1]
    cfRefImg1   = refAndMasks[2]
    nimg, Ly, Lx = data.shape
    maxregshift = np.round(ops['maxregshiftNR'] *np.maximum(Ly, Lx))
    LyMax = np.diff(np.array(ops['yblock']))
    ly = int(np.diff(ops['yblock'][0]))
    lx = int(np.diff(ops['xblock'][0]))
    lyhalf = int(np.floor(ly/2))
    lxhalf = int(np.floor(lx/2))
    lcorr = int(np.minimum(maxregshift, np.floor(np.minimum(ly,lx)/2.)-lpad))
    nb = len(ops['yblock'])
    nblocks = ops['nblocks']
    ymax1 = np.zeros((nimg,nb),np.float32)
    cmax1 = np.zeros((nimg,nb),np.float32)
    xmax1 = np.zeros((nimg,nb),np.float32)
    data_block = np.zeros((nb,nimg,ly,lx),np.float32)
    # compute phase-correlation of blocks
    for n in range(nb):
        yind = ops['yblock'][n]
        yind = np.arange(yind[0],yind[-1]+1).astype(int)
        xind = ops['xblock'][n]
        xind = np.arange(xind[0],xind[-1]+1).astype(int)
        data_block[n,:,:,:] = data[np.ix_(np.arange(0,nimg).astype(int),yind,xind)]
    cc1 = correlation_map(data_block, refAndMasks)
    for n in range(nb):
        cc = cc1[n,:,:,:]
        ymax, xmax, cmax = register.getXYup(cc, (lcorr,lpad, lyhalf, lxhalf), ops)
        ymax1[:,n] = ymax
        xmax1[:,n] = xmax
        cmax1[:,n] = cmax
    # smooth cc across blocks if sig>0
    sig = 0
    if sig>0:
        cc1 = np.reshape(cc1,(nimg,ly,lx,nblocks[0],nblocks[1]))
        cc1 = gaussian_filter(cc1, [0,0,0,sig,sig])
        cc1 = np.reshape(cc1,(nimg,ly,lx,nb))
        for n in range(nb):
            ymax, xmax, cmax = register.getXYup(cc1[:,:,:,n], (lcorr,lpad, lyhalf, lxhalf), ops)
            ymax1[:,n] = ymax
            xmax1[:,n] = xmax
            cmax1[:,n] = cmax
    Y = shift_data((data, ymax1, xmax1, ops))
    return Y, ymax1, xmax1, cmax1

def shift_data(inputs):
    ''' piecewise affine transformation of data using shifts from phasecorr_worker '''
    data,ymax,xmax,ops = inputs
    nblocks = ops['nblocks']
    if data.ndim<3:
        data = data[np.newaxis,:,:]
    nimg,Ly,Lx = data.shape
    Y = np.zeros(data.shape, np.float32)
    nb = ymax.shape[1]
    ymax = np.reshape(ymax, (nimg,nblocks[0], nblocks[1]))
    xmax = np.reshape(xmax, (nimg,nblocks[0], nblocks[1]))
    # make arrays of control points for piecewise-affine transform
    # includes centers of blocks AND edges of blocks
    # note indices are flipped for control points
    # block centers
    y = np.round(np.unique(np.array(ops['yblock']).mean(axis=1)))
    y = np.hstack((0,y,Ly-1))
    x = np.round(np.unique(np.array(ops['xblock']).mean(axis=1)))
    x = np.hstack((0,x,Lx-1))
    mshx,mshy = np.meshgrid(np.arange(0,Ly),np.arange(0,Lx))

    print(ops['yblock'])
    print(ops['xblock'])
    print(ymax.shape)
    # loop over frames
    for t in range(nimg):
        I = data[t,:,:]
        ymax0 = np.pad(ymax[t,:,:],((1,),(1,)),mode='edge')
        xmax0 = np.pad(xmax[t,:,:],((1,),(1,)),mode='edge')
        print(y.size )
        print(x.size )
        print(ymax0.shape)
        fy = interp2d(y,x,ymax0.T,kind='linear')
        fx = interp2d(y,x,xmax0.T,kind='linear')
        # interpolated values on grid with all points
        fyout = fy(np.arange(0,Ly),np.arange(0,Lx)) + mshy
        fxout = fx(np.arange(0,Ly),np.arange(0,Lx)) + mshx
        coords = np.concatenate((fyout[np.newaxis,:],fxout[np.newaxis,:]))
        Iw = warp(I,coords, order=0, clip=False, preserve_range=True)
        Y[t,:,:] = Iw
    return Y

def register_myshifts(ops, data, ymax, xmax):
    if ops['num_workers']<0:
        dreg = shift_data((data, ymax, xmax, ops))
    else:
        if ops['num_workers']<1:
            ops['num_workers'] = int(multiprocessing.cpu_count()/2)
        num_cores = ops['num_workers']
        nimg = data.shape[0]
        nbatch = int(np.ceil(nimg/float(num_cores)))
        #nbatch = 50
        inputs = np.arange(0, nimg, nbatch)
        irange = []
        dsplit = []
        for i in inputs:
            ilist = i + np.arange(0,np.minimum(nbatch, nimg-i))
            irange.append(i + np.arange(0,np.minimum(nbatch, nimg-i)))
            dsplit.append([data[ilist,:, :], ymax[ilist], xmax[ilist], ops])
        with Pool(num_cores) as p:
            results = p.map(shift_data, dsplit)

        dreg = np.zeros_like(data)
        for i in range(0,len(results)):
            dreg[irange[i], :, :] = results[i]
    return dreg
