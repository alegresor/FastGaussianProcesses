import torch 
import numpy as np 
import scipy.stats 
import os
import qmcpy
import itertools
from typing import List

class _XXbSeq(object):
    def __init__(self, fgp, seq):
        self.fgp = fgp
        self.seq = seq
        self.n = 0
        self.x = torch.empty((0,seq.d))
        self.xb = torch.empty((0,seq.d),dtype=self.fgp._XBDTYPE)
    def __getitem__(self, i):
        if isinstance(i,int): i = slice(None,i,None)
        if isinstance(i,torch.Tensor):
            assert i.numel()==1 and isinstance(i,torch.int64)
            i = slice(None,i.item(),None)
        assert isinstance(i,slice)
        if i.stop>self.n:
            x_next,xb_next = self.fgp._sample(self.seq,self.n,i.stop)
            if x_next.data_ptr()==xb_next.data_ptr():
                self.x = self.xb = torch.vstack([self.x,x_next])
            else:
                self.x = torch.vstack([self.x,x_next])
                self.xb = torch.vstack([self.xb,xb_next])
            self.n = i.stop
        return self.x[i],self.xb[i]

class _K1PartsSeq(object):
    def __init__(self, fgp, xxb_seq_first, xxb_seq_second):
        self.fgp = fgp
        self.xxb_seq_first = xxb_seq_first
        self.xxb_seq_second = xxb_seq_second
        self.k1parts = torch.empty((0,fgp.d))
        self.n = 0
    def __getitem__(self, i):
        if isinstance(i,int): i = slice(None,i,None)
        if isinstance(i,torch.Tensor):
            assert i.numel()==1 and isinstance(i,torch.int64)
            i = slice(None,i.item(),None)
        assert isinstance(i,slice)
        if i.stop>self.n:
            _,xb_next = self.xxb_seq_first[self.n:i.stop]
            _,xb0 = self.xxb_seq_second[:1]
            k1parts_next = self.fgp._kernel_parts(xb_next,xb0)
            self.k1parts = torch.vstack([self.k1parts,k1parts_next])
            self.n = i.stop
        return self.k1parts[i]

class _LamCaches(object):
    def __init__(self, fgp, l0, l1):
        self.fgp = fgp
        self.l0 = l0
        self.l1 = l1
        self.m_min,self.m_max = -1,-1
        self.raw_scale_freeze_list = [None]
        self.raw_lengthscales_freeze_list = [None]
        self.raw_noise_freeze_list = [None]
        self._freeze(0)
        self.lam_list = [torch.empty(0,dtype=self.fgp._FTOUTDTYPE)]
    def _frozen_equal(self, i):
        return (
            (self.fgp.raw_scale==self.raw_scale_freeze_list[i]).all() and 
            (self.fgp.raw_lengthscales==self.raw_lengthscales_freeze_list[i]).all() and 
            (self.fgp.raw_noise==self.raw_noise_freeze_list[i]).all())
    def _force_recompile(self):
        return os.environ.get("FASTGP_FORCE_RECOMPILE")=="True" and (
            self.fgp.raw_scale.requires_grad or 
            self.fgp.raw_lengthscales.requires_grad or 
            self.fgp.raw_noise.requires_grad)
    def _freeze(self, i):
        self.raw_scale_freeze_list[i] = self.fgp.raw_scale.clone()
        self.raw_lengthscales_freeze_list[i] = self.fgp.raw_lengthscales.clone()
        self.raw_noise_freeze_list[i] = self.fgp.raw_noise.clone()
    def __getitem__no_delete(self, m):
        if isinstance(m,torch.Tensor):
            assert m.numel()==1 and isinstance(m,torch.int64)
            m = m.item()
        assert isinstance(m,int)
        assert m>=self.m_min, "old lambda are not retained after updating"
        if self.m_min==-1 and m>=0:
            k1 = self.fgp._kernel_from_parts(self.fgp.get_k1parts(self.l0,self.l1,n=2**m))
            if self.l0==self.l1:
                k1[0] += self.fgp.noise
            self.lam_list = [self.fgp.ft(k1)]
            self._freeze(0)
            self.m_min = self.m_max = m
            return self.lam_list[0]
        assert self.m_min>=self.fgp.m[self.l0], "requires self.m_min = %d >= self.fgp.m[self.l0] = %d"%(self.m_min,self.fgp.m[self.l0])
        if m==self.m_min:
            if not self._frozen_equal(0) or self._force_recompile():
                k1 = self.fgp._kernel_from_parts(self.fgp.k1parts_seq[self.l0,self.l1][:2**self.m_min])
                k1[0] += self.fgp.noise
                self.lam_list[0] = self.fgp.ft(k1)
                self._freeze(0)
            return self.lam_list[0]
        if m>self.m_max:
            self.lam_list += [torch.empty(2**mm,dtype=self.fgp._FTOUTDTYPE) for mm in range(self.m_max+1,m+1)]
            self.raw_scale_freeze_list += [torch.empty_like(self.raw_scale_freeze_list[0])]*(m-self.m_max)
            self.raw_lengthscales_freeze_list += [torch.empty_like(self.raw_lengthscales_freeze_list[0])]*(m-self.m_max)
            self.raw_noise_freeze_list += [torch.empty_like(self.raw_noise_freeze_list[0])]*(m-self.m_max)
            self.m_max = m
        midx = m-self.m_min
        if not self._frozen_equal(midx) or self._force_recompile():
            omega_m = self.fgp.get_omega(m-1)
            k1_m = self.fgp._kernel_from_parts(self.fgp.k1parts_seq[self.l0,self.l1][2**(m-1):2**m])
            lam_m = self.fgp.ft(k1_m)
            omega_lam_m = omega_m*lam_m
            lam_m_prev = self.__getitem__no_delete(m-1)
            self.lam_list[midx] = torch.hstack([lam_m_prev+omega_lam_m,lam_m_prev-omega_lam_m])/np.sqrt(2)
            if os.environ.get("FASTGP_DEBUG")=="True":
                k1_full = self.fgp._kernel_from_parts(self.fgp.k1parts_seq[self.l0,self.l1][:2**m])
                lam_full = self.fgp.ft(k1_full)
                assert torch.allclose(self.lam_list[midx],lam_full,atol=1e-7,rtol=0)
            self._freeze(midx)
        return self.lam_list[midx]
    def __getitem__(self, m):
        lam = self.__getitem__no_delete(m)
        while self.m_min<self.fgp.m[self.l0]:
            del self.lam_list[0]
            del self.raw_scale_freeze_list[0]
            del self.raw_lengthscales_freeze_list[0]
            del self.raw_noise_freeze_list[0]
            self.m_min += 1
        return lam

class _TaskCovCache(object):
    def __init__(self, fgp):
        self.fgp = fgp 
        self.num_tasks_range = torch.arange(self.fgp.num_tasks,device=self.fgp.device)
    def _frozen_equal(self):
        return (
            (self.fgp.raw_factor_task_kernel==self.raw_factor_task_kernel_freeze).all() and 
            (self.fgp.raw_noise_task_kernel==self.raw_noise_task_kernel_freeze).all())
    def _force_recompile(self):
        return os.environ.get("FASTGP_FORCE_RECOMPILE")=="True" and (
            self.fgp.raw_factor_task_kernel.requires_grad or 
            self.fgp.raw_noise_task_kernel.requires_grad)
    def _freeze(self):
        self.raw_factor_task_kernel_freeze = self.fgp.raw_factor_task_kernel.clone()
        self.raw_noise_task_kernel_freeze = self.fgp.raw_noise_task_kernel.clone()
    def __call__(self):
        if not hasattr(self,"kmat") or not self._frozen_equal() or self._force_recompile():
            self.kmat = self.fgp.factor_task_kernel@self.fgp.factor_task_kernel.T
            self.kmat[self.num_tasks_range,self.num_tasks_range] += self.fgp.noise_task_kernel
            self._freeze()
        return self.kmat

class _YtildeCache(object):
    def __init__(self, fgp, l):
        self.fgp = fgp
        self.l = l
        self.n = -1
    def __call__(self):
        if not hasattr(self,"ytilde") or self.fgp.n[self.l]<=1:
            self.ytilde = self.fgp.ft(self.fgp.y[self.l]) if self.fgp.n[self.l]>1 else self.fgp.y[self.l].clone().to(self.fgp._FTOUTDTYPE)
            self.n = self.fgp.n[self.l].clone()
            return self.ytilde
        while self.n!=self.fgp.n[self.l]:
            n_double = 2*self.n
            ytilde_next = self.fgp.ft(self.fgp.y[...,self.n:n_double])
            omega_m = self.fgp.get_omega(int(np.log2(self.n)))
            omega_ytilde_next = omega_m*ytilde_next
            self.ytilde = torch.hstack([self.ytilde+omega_ytilde_next,self.ytilde-omega_ytilde_next])/np.sqrt(2)
            if os.environ.get("FASTGP_DEBUG")=="True":
                ytilde_ref = self.fgp.ft(self.fgp.y[:n_double])
                assert torch.allclose(self.ytilde,ytilde_ref,atol=1e-7,rtol=0)
            self.n = n_double
        return self.ytilde

class _InverseLogDetCache(object):
    def __init__(self, fgp, n):
        self.fgp = fgp
        self.n = n
        self.task_order = self.n.argsort(descending=True)
        self.inv_task_order = self.task_order.argsort()
    def _frozen_equal(self):
        return (
            (self.fgp.raw_scale==self.raw_scale_freeze).all() and 
            (self.fgp.raw_lengthscales==self.raw_lengthscales_freeze).all() and 
            (self.fgp.raw_noise==self.raw_noise_freeze).all() and 
            (self.fgp.raw_factor_task_kernel==self.raw_factor_task_kernel_freeze).all() and 
            (self.fgp.raw_noise_task_kernel==self.raw_noise_task_kernel_freeze).all())
    def _force_recompile(self):
        return os.environ.get("FASTGP_FORCE_RECOMPILE")=="True" and (
            self.fgp.raw_scale.requires_grad or 
            self.fgp.raw_lengthscales.requires_grad or 
            self.fgp.raw_noise.requires_grad or 
            self.fgp.raw_factor_task_kernel.requires_grad or 
            self.fgp.raw_noise_task_kernel.requires_grad)
    def _freeze(self):
        self.raw_scale_freeze = self.fgp.raw_scale.clone()
        self.raw_lengthscales_freeze = self.fgp.raw_lengthscales.clone()
        self.raw_noise_freeze = self.fgp.raw_noise.clone()
        self.raw_factor_task_kernel_freeze = self.fgp.raw_factor_task_kernel.clone()
        self.raw_noise_task_kernel_freeze = self.fgp.raw_noise_task_kernel.clone()
    def __call__(self):
        if not hasattr(self,"inv") or not self._frozen_equal() or self._force_recompile():
            n = self.n[self.task_order]
            kmat_tasks = self.fgp.gram_matrix_tasks
            lams = np.empty((self.fgp.num_tasks,self.fgp.num_tasks),dtype=object)
            for l0 in range(self.fgp.num_tasks):
                to0 = self.task_order[l0]
                for l1 in range(l0,self.fgp.num_tasks):
                    to1 = self.task_order[l1]
                    lams[l0,l1] = torch.sqrt(n[l1])*kmat_tasks[to0,to1]*self.fgp.get_lam(to0,to1,n[l0])
            self.logdet = torch.log(torch.abs(lams[0,0])).sum()
            A = (1/lams[0,0])[None,None,:]
            for l in range(1,self.fgp.num_tasks):
                B = torch.cat([lams[k,l] for k in range(l)],dim=0).reshape((-1,n[l]))
                Bvec = B.reshape((A.size(1),-1))
                T = (Bvec*A).sum(1).reshape((-1,n[l]))
                M = (B.conj()*T).sum(0)
                S = lams[l,l]-M
                self.logdet += torch.log(torch.abs(S)).sum()
                P = T/S
                C = P[:,None,:]*(T[None,:,:].conj())
                r = A.size(-1)//C.size(-1)
                ii = torch.arange(A.size(0))
                jj = torch.arange(A.size(-1))
                ii0,ii1,ii2 = torch.meshgrid(ii,ii,jj,indexing="ij")
                ii0,ii1,ii2 = ii0.ravel(),ii1.ravel(),ii2.ravel()
                jj0 = ii2%C.size(-1)
                jj1 = ii2//C.size(-1)
                C[ii0*r+jj1,ii1*r+jj1,jj0] += A[ii0,ii1,ii2]
                ur = torch.cat([C,-P[:,None,:]],dim=1)
                br = torch.cat([-P.conj()[None,:,:],1/S[None,None,:]],dim=1)
                A = torch.cat([ur,br],dim=0)
            if os.environ.get("FASTGP_DEBUG")=="True":
                lammats = np.empty((self.fgp.num_tasks,self.fgp.num_tasks),dtype=object)
                for l0 in range(self.fgp.num_tasks):
                    for l1 in range(l0,self.fgp.num_tasks):
                        lammats[l0,l1] = (lams[l0,l1].reshape((-1,n[l1],1))*torch.eye(n[l1])).reshape((-1,n[l1]))
                        if l0==l1: continue 
                        lammats[l1,l0] = lammats[l0,l1].conj().T
                lammat = torch.vstack([torch.hstack(lammats[i].tolist()) for i in range(self.fgp.num_tasks)])
                assert torch.allclose(torch.logdet(lammat).real,self.logdet)
                Afull = torch.vstack([torch.hstack([A[l0,l1]*torch.eye(A.size(-1)) for l1 in range(A.size(1))]) for l0 in range(A.size(0))])
                assert torch.allclose(torch.linalg.inv(lammat),Afull,rtol=1e-4)
            self._freeze()
            self.inv = A
        return self.inv,self.logdet
    def gram_matrix_solve(self, y):
        yogdim = y.ndim 
        if yogdim==1:
            y = y[:,None] 
        assert y.size(-2)==self.n.sum() 
        z = y.transpose(dim0=-2,dim1=-1)
        zs = z.split(self.n.tolist(),dim=-1)
        zst = [self.fgp.ft(zs[i]) for i in range(self.fgp.num_tasks)]
        zst,_ = self._gram_matrix_solve_tilde_to_tilde(zst)
        zs = [self.fgp.ift(zst[i]).real for i in range(self.fgp.num_tasks)]
        z = torch.cat(zs,dim=-1).transpose(dim0=-2,dim1=-1)
        if os.environ.get("FASTGP_DEBUG")=="True":
            _,logdet = self()
            kmat_tasks = self.fgp.gram_matrix_tasks
            kmat = torch.vstack([torch.hstack([kmat_tasks[ell0,ell1]*self.fgp.kernel(self.fgp.get_x(ell0,self.n[ell0])[:,None,:],self.fgp.get_x(ell1,self.n[ell1])[None,:,:]) for ell1 in range(self.fgp.num_tasks)]) for ell0 in range(self.fgp.num_tasks)])
            kmat += self.fgp.noise*torch.eye(kmat.size(0))
            assert torch.allclose(logdet,torch.logdet(kmat))
            ztrue = torch.linalg.solve(kmat,y)
            assert torch.allclose(ztrue,z,atol=1e-3)
        if yogdim==1:
            z = z[:,0]
        return z
    def _gram_matrix_solve_tilde_to_tilde(self, zst):
        inv,logdet = self()
        zsto = [zst[o] for o in self.task_order]
        z = torch.cat(zsto,dim=-1).reshape(list(zsto[0].shape[:-1])+[1,-1,self.n.min()])
        z = (z*inv).sum(-2)
        z = z.reshape(list(z.shape[:-2])+[-1])
        zsto = z.split(self.n[self.task_order].tolist(),dim=-1)
        zst = [zsto[o] for o in self.inv_task_order]
        return zst,logdet

class _CoeffsCache(object):
    def __init__(self, fgp):
        self.fgp = fgp
    def _frozen_equal(self):
        return (
            (self.fgp.raw_scale==self.raw_scale_freeze).all() and 
            (self.fgp.raw_lengthscales==self.raw_lengthscales_freeze).all() and 
            (self.fgp.raw_noise==self.raw_noise_freeze).all() and 
            (self.fgp.raw_factor_task_kernel==self.raw_factor_task_kernel_freeze).all() and 
            (self.fgp.raw_noise_task_kernel==self.raw_noise_task_kernel_freeze).all())
    def _force_recompile(self):
        return os.environ.get("FASTGP_FORCE_RECOMPILE")=="True" and (
            self.fgp.raw_scale.requires_grad or 
            self.fgp.raw_lengthscales.requires_grad or 
            self.fgp.raw_noise.requires_grad or 
            self.fgp.raw_factor_task_kernel.requires_grad or 
            self.fgp.raw_noise_task_kernel.requires_grad)
    def _freeze(self):
        self.raw_scale_freeze = self.fgp.raw_scale.clone()
        self.raw_lengthscales_freeze = self.fgp.raw_lengthscales.clone()
        self.raw_noise_freeze = self.fgp.raw_noise.clone()
        self.raw_factor_task_kernel_freeze = self.fgp.raw_factor_task_kernel.clone()
        self.raw_noise_task_kernel_freeze = self.fgp.raw_noise_task_kernel.clone()
    def __call__(self):
        if not hasattr(self,"coeffs") or (self.n!=self.fgp.n).any() or not self._frozen_equal() or self._force_recompile():
            self.coeffs = self.fgp.get_inv_log_det_cache().gram_matrix_solve(torch.cat(self.fgp.y,dim=0))
            self._freeze()
            self.n = self.fgp.n.clone()
        return self.coeffs 

class _FastMultiTaskGP(torch.nn.Module):
    def __init__(self,
        seqs,
        num_tasks, 
        alpha,
        scale,
        lengthscales,
        noise,
        factor_task_kernel,
        noise_task_kernel,
        device,
        tfs_scale,
        tfs_lengthscales,
        tfs_noise,
        tfs_factor_task_kernel,
        tfs_noise_task_kernel,
        requires_grad_scale, 
        requires_grad_lengthscales, 
        requires_grad_noise,
        requires_grad_factor_task_kernel,
        requires_grad_noise_task_kernel,
        ft,
        ift,
        ):
        super().__init__()
        assert torch.get_default_dtype()==torch.float64, "fast transforms do not work without torch.float64 precision" 
        assert isinstance(num_tasks,int) and num_tasks>0
        self.num_tasks = num_tasks
        self.device = torch.device(device)
        assert isinstance(seqs,np.ndarray) and seqs.shape==(self.num_tasks,)
        self.d = seqs[0].d
        assert all(seqs[i].d==self.d for i in range(self.num_tasks))
        self.seqs = seqs
        self.n = torch.zeros(self.num_tasks,dtype=int)
        self.m = -1*torch.ones(self.num_tasks,dtype=int)
        assert (np.isscalar(alpha) and alpha%1==0) or (isinstance(alpha,torch.Tensor) and alpha.shape==(self,d,)), "alpha should be an int or a torch.Tensor of length d"
        if np.isscalar(alpha):
            alpha = int(alpha)*torch.ones(self.d,dtype=int,device=self.device)
        self.alpha = alpha
        assert np.isscalar(scale) and scale>0, "scale should be a positive float"
        scale = torch.tensor(scale,device=self.device)
        assert len(tfs_scale)==2 and callable(tfs_scale[0]) and callable(tfs_scale[1]), "tfs_scale should be a tuple of two callables, the transform and inverse transform"
        self.tf_scale = tfs_scale[1]
        self.raw_scale = torch.nn.Parameter(tfs_scale[0](scale),requires_grad=requires_grad_scale)
        assert (np.isscalar(lengthscales) and lengthscales>0) or (isinstance(lengthscales,torch.Tensor) and lengthscales.shape==(self,d) and (lengthscales>0).all()), "lengthscales should be a float or torch.Tensor of length d and must be postivie"
        if np.isscalar(lengthscales): 
            lengthscales = lengthscales*torch.ones(self.d,device=self.device)
        assert len(tfs_lengthscales)==2 and callable(tfs_lengthscales[0]) and callable(tfs_lengthscales[1]), "tfs_lengthscales should be a tuple of two callables, the transform and inverse transform"
        self.tf_lengthscales = tfs_lengthscales[1]
        self.raw_lengthscales = torch.nn.Parameter(tfs_lengthscales[0](lengthscales),requires_grad=requires_grad_lengthscales)
        assert np.isscalar(noise) and noise>0, "noise should be a positive float"
        noise = torch.tensor(noise,device=self.device)
        assert len(tfs_noise)==2 and callable(tfs_noise[0]) and callable(tfs_noise[1]), "tfs_scale should be a tuple of two callables, the transform and inverse transform"
        assert factor_task_kernel is None or (isinstance(factor_task_kernel,int) and 0<=factor_task_kernel<=self.num_tasks) or (isinstance(factor_task_kernel,torch.Tensor) and factor_task_kernel.ndim==2 and factor_task_kernel.size(0)==self.num_tasks and factor_task_kernel.size(1)<=self.num_tasks), "factor_task_kernel should be a non-negative int less than num_tasks or a num_tasks x r torch.Tensor with r<=num_tasks" 
        self.tf_noise = tfs_noise[1]
        self.raw_noise = torch.nn.Parameter(tfs_noise[0](noise),requires_grad=requires_grad_noise)
        if factor_task_kernel is None:
            factor_task_kernel = self.num_tasks
        if isinstance(factor_task_kernel,int):
            factor_task_kernel = torch.ones((self.num_tasks,factor_task_kernel),device=self.device)
        assert len(tfs_factor_task_kernel)==2 and callable(tfs_factor_task_kernel[0]) and callable(tfs_factor_task_kernel[1]), "tfs_factor_task_kernel should be a tuple of two callables, the transform and inverse transform"
        self.tf_factor_task_kernel = tfs_factor_task_kernel[1]
        self.raw_factor_task_kernel = torch.nn.Parameter(tfs_factor_task_kernel[0](factor_task_kernel),requires_grad=requires_grad_factor_task_kernel)
        assert (np.isscalar(noise_task_kernel) and noise_task_kernel>0) or (isinstance(noise_task_kernel,torch.Tensor) and noise_task_kernel.shape==(self.num_tasks,) and (noise_task_kernel>0).all()), "noise_task_kernel should be a scalar or torch.Tensor of length num_tasks and must be positive"
        if np.isscalar(noise_task_kernel):
            noise_task_kernel = noise_task_kernel*torch.ones(self.num_tasks,device=self.device)
        assert len(tfs_noise_task_kernel)==2 and callable(tfs_noise_task_kernel[0]) and callable(tfs_noise_task_kernel[1]), "tfs_noise_task_kernel should be a tuple of two callables, the transform and inverse transform"
        self.tf_noise_task_kernel = tfs_noise_task_kernel[1]
        self.raw_noise_task_kernel = torch.nn.Parameter(tfs_noise_task_kernel[0](noise_task_kernel),requires_grad=requires_grad_noise_task_kernel)
        self.ft = ft
        self.ift = ift
        self.xxb_seqs = np.array([_XXbSeq(self,self.seqs[i]) for i in range(self.num_tasks)],dtype=object)
        self.k1parts_seq = np.array([[_K1PartsSeq(self,self.xxb_seqs[l0],self.xxb_seqs[l1]) for l1 in range(self.num_tasks)] for l0 in range(self.num_tasks)],dtype=object)
        self.lam_caches = np.array([[_LamCaches(self,l0,l1) for l1 in range(self.num_tasks)] for l0 in range(self.num_tasks)],dtype=object)
        self.ytilde_cache = np.array([_YtildeCache(self,i) for i in range(self.num_tasks)],dtype=object)
        self.task_cov_cache = _TaskCovCache(self)
        self.coeffs_cache = _CoeffsCache(self)
        self.inv_log_det_cache_dict = {}
        self.y = [torch.empty(0) for l in range(self.num_tasks)]
    @property
    def scale(self):
        return self.tf_scale(self.raw_scale)
    @property
    def lengthscales(self):
        return self.tf_lengthscales(self.raw_lengthscales)
    @property
    def noise(self):
        return self.tf_noise(self.raw_noise)
    @property
    def factor_task_kernel(self):
        return self.tf_factor_task_kernel(self.raw_factor_task_kernel)
    @property
    def noise_task_kernel(self):
        return self.tf_noise_task_kernel(self.raw_noise_task_kernel)
    @property 
    def gram_matrix_tasks(self):
        return self.task_cov_cache()
    @property 
    def coeffs(self):
        return self.coeffs_cache()
    def get_x(self, task, n=None):
        assert 0<=task<self.num_tasks
        if n is None: n = self.n[task]
        assert n>=0
        x,xb = self.xxb_seqs[task][:n]
        return x
    def get_xb(self, task, n=None):
        assert 0<=task<self.num_tasks
        if n is None: n = self.n[task]
        assert n>=0
        x,xb = self.xxb_seqs[task][:n]
        return xb
    def get_lam(self, task0, task1, n=None):
        assert 0<=task0<self.num_tasks
        assert 0<=task1<self.num_tasks
        if n is None: m = int(self.m[task0])
        else: m = -1 if n==0 else int(np.log2(int(n)))
        assert m>=0
        return self.lam_caches[task0,task1][m]
    def get_k1parts(self, task0, task1, n=None):
        assert 0<=task0<self.num_tasks
        assert 0<=task1<self.num_tasks
        if n is None: n = self.n[task0]
        assert n>=0
        return self.k1parts_seq[task0,task1][:n]
    def get_ytilde(self, task):
        assert 0<=task<self.num_tasks
        return self.ytilde_cache[task]()
    def get_inv_log_det_cache(self, n=None):
        if n is None: n = self.n
        assert isinstance(n,torch.Tensor) and n.shape==(self.num_tasks,) and (n>=self.n).all()
        ntup = tuple(n.tolist())
        if ntup not in self.inv_log_det_cache_dict.keys():
            self.inv_log_det_cache_dict[ntup] = _InverseLogDetCache(self,n)
        for key in list(self.inv_log_det_cache_dict.keys()):
            if (torch.tensor(key)<self.n).any():
                del self.inv_log_det_cache_dict[key]
        return self.inv_log_det_cache_dict[ntup]
    def get_inv_log_det(self, n=None):
        inv_log_det_cache = self.get_inv_log_det_cache(n)
        return inv_log_det_cache()
    def get_x_next(self, n:torch.Tensor, task:torch.Tensor=None):
        """
        Get next sampling locations. 

        Args:
            n (torch.Tensor[num_tasks]): maximum sample index for each task
            task (torch.Tensor): task indices, should have the same length as n
        
        Returns:
            x_next (List[num_tasks] of torch.Tensor[n[task[i]]-self.n[task[i]],d]): next samples in the sequence
        """
        if isinstance(n,int): n = torch.tensor([n],dtype=int) 
        if isinstance(n,list): n = torch.tensor(n,dtype=int)
        if task is None: task = torch.arange(self.num_tasks,dtype=int)
        inttask = isinstance(task,int)
        if inttask: task = torch.tensor([task],dtype=int)
        if isinstance(task,list): task = torch.tensor(task,dtype=int)
        assert isinstance(n,torch.Tensor) and isinstance(task,torch.Tensor) and n.ndim==task.ndim==1 and len(n)==len(task)
        assert (n>=self.n[task]).all() and torch.logical_or(n==0,n&(n-1)==0).all(), "maximum sequence index must be a power of 2 greater than the current number of samples"
        x_next = [self.xxb_seqs[l][self.n[l]:n[i]][0] for i,l in enumerate(task)]
        return x_next[0] if inttask else x_next
    def add_y_next(self, y_next:List, task:torch.Tensor=None):
        """
        Increase the sample size to `n`. 

        Args:
            y_next (List[num_tasks] of torch.Tensor[...,n[i]-self.n[i]]): new function evaluations for each task
            task (torch.Tensor): task indices, should have the same length as n
        """
        if isinstance(y_next,torch.Tensor): y_next = [y_next]
        if task is None: task = torch.arange(self.num_tasks,dtype=int)
        if isinstance(task,int): task = torch.tensor([task],dtype=int)
        if isinstance(task,list): task = torch.tensor(task,dtype=int)
        assert isinstance(y_next,list) and isinstance(task,torch.Tensor) and task.ndim==1 and len(y_next)==len(task)
        assert all(y_next[i].shape[:-1]==y_next[0].shape[:-1] for i in range(len(y_next)))
        self.d_out = y_next[0][...,0].numel()
        for i,l in enumerate(task):
            self.y[l] = torch.cat([self.y[l],y_next[i]],-1)
        self.n = torch.tensor([self.y[i].size(-1) for i in range(self.num_tasks)],dtype=int)
        assert torch.logical_or(self.n==0,(self.n&(self.n-1)==0)).all(), "total samples must be power of 2"
        self.m = torch.where(self.n==0,-1,torch.log2(self.n)).to(int)
    def _kernel_parts(self, x, z):
        return self._kernel_parts_from_delta(self._ominus(x,z))
    def _kernel_from_parts(self, parts):
        return self.scale*(1+self.lengthscales*parts).prod(-1)
    def _kernel_from_delta(self, delta):
        return self._kernel_from_parts(self._kernel_parts_from_delta(delta))
    def kernel(self, x:torch.Tensor, z:torch.Tensor):
        """
        Evaluate kernel

        Args:
            x (torch.Tensor[N,d]): first argument to kernel  
            z (torch.Tensor[M,d]): second argument to kernel 
        
        Returns:
            kmat (torch.Tensor[N,M]): matrix of kernel evaluations
        """
        return self._kernel_from_parts(self._kernel_parts(x,z))
    def post_mean(self, x:torch.Tensor, task:torch.Tensor=None, eval:bool=True):
        """
        Posterior mean. 

        Args:
            x (torch.Tensor[N,d]): sampling locations
            eval (bool): if `True`, disable gradients, otherwise use `torch.is_grad_enabled()`
            task (torch.Tensor): task indices
        
        Returns:
            pmean (torch.Tensor[*batch_shape,N]): posterior mean where `batch_shape` is inferred from `y=f(x)`
        """
        coeffs = self.coeffs
        kmat_tasks = self.gram_matrix_tasks
        if eval:
            incoming_grad_enabled = torch.is_grad_enabled()
            torch.set_grad_enabled(False)
        assert x.ndim==2 and x.size(1)==self.d, "x must a torch.Tensor with shape (-1,d)"
        if task is None: task = torch.arange(self.num_tasks)
        inttask = isinstance(task,int)
        if inttask: task = torch.tensor([task],dtype=int)
        if isinstance(task,list): task = torch.tensor(task,dtype=int)
        assert task.ndim==1 and (task>=0).all() and (task<self.num_tasks).all()
        kmat = torch.cat([self.kernel(x[:,None,:],self.get_xb(l)[None,:,:])*kmat_tasks[task,l,None,None] for l in range(self.num_tasks)],dim=-1)
        pmean = torch.einsum("til,...l->...ti",kmat,coeffs)
        if eval:
            torch.set_grad_enabled(incoming_grad_enabled)
        return pmean[...,0,:] if inttask else pmean
    def post_cov(self, x0:torch.Tensor, x1:torch.Tensor, task0:torch.Tensor=None, task1:torch.Tensor=None, n:torch.Tensor=None, eval:bool=True):
        """
        Posterior covariance. 
        If `torch.equal(x,z)` then the diagonal of the covariance matrix is forced to be non-negative. 

        Args:
            x (torch.Tensor[N,d]): left sampling locations
            z (torch.Tensor[M,d]): right sampling locations
            task0 (torch.Tensor): left task indices
            task1 (torch.Tensor): right task indices
            n (torch.Tensor): Number of points at which to evaluate the posterior cubature variance. Defaults to `n=self.n`. `n` must be powers of 2.  
            eval (bool): if `True`, disable gradients, otherwise use `torch.is_grad_enabled()`
        
        Returns:
            pcov (torch.Tensor[N,M]): posterior covariance matrix
        """
        if n is None: n = self.n
        assert isinstance(n,torch.Tensor) and (n&(n-1)==0).all() and (n>=self.n).all(), "require n are all power of two greater than or equal to self.n"
        assert x0.ndim==2 and x0.size(1)==self.d, "x must a torch.Tensor with shape (-1,d)"
        assert x1.ndim==2 and x1.size(1)==self.d, "z must a torch.Tensor with shape (-1,d)"
        kmat_tasks = self.gram_matrix_tasks
        if eval:
            incoming_grad_enabled = torch.is_grad_enabled()
            torch.set_grad_enabled(False)
        if task0 is None: task0 = torch.arange(self.num_tasks)
        inttask0 = isinstance(task0,int)
        if inttask0: task0 = torch.tensor([task0],dtype=int)
        if isinstance(task0,list): task0 = torch.tensor(task0,dtype=int)
        assert task0.ndim==1 and (task0>=0).all() and (task0<self.num_tasks).all()
        if task1 is None: task1 = torch.arange(self.num_tasks)
        inttask1 = isinstance(task1,int)
        if inttask1: task1 = torch.tensor([task1],dtype=int)
        if isinstance(task1,list): task1 = torch.tensor(task1,dtype=int)
        assert task1.ndim==1 and (task1>=0).all() and (task1<self.num_tasks).all()
        equal = torch.equal(x0,x1) and torch.equal(task0,task1)
        kmat_new = self.kernel(x0[:,None,:],x1[None,:,:])*kmat_tasks[task0,:][:,task1][:,:,None,None]
        kmat1 = torch.cat([self.kernel(x0[:,None,:],self.get_xb(l,n[l])[None,:,:])*kmat_tasks[task0,l,None,None] for l in range(self.num_tasks)],dim=-1)
        kmat2 = kmat1 if equal else torch.cat([self.kernel(x1[:,None,:],self.get_xb(l,n[l])[None,:,:])*kmat_tasks[task1,l,None,None] for l in range(self.num_tasks)],dim=-1)
        t = self.get_inv_log_det_cache(n).gram_matrix_solve(kmat2.transpose(dim0=-2,dim1=-1)).transpose(dim0=-2,dim1=-1)
        kmat = kmat_new-torch.einsum("ikl,jml->ijkm",kmat1,t)
        if equal:
            tmesh,nmesh = torch.meshgrid(torch.arange(kmat.size(0),device=self.device),torch.arange(x0.size(0),device=x0.device),indexing="ij")            
            tidx,nidx = tmesh.ravel(),nmesh.ravel()
            diag = kmat[tidx,tidx,nidx,nidx]
            diag[diag<0] = 0 
            kmat[tidx,tidx,nidx,nidx] = diag 
        if eval:
            torch.set_grad_enabled(incoming_grad_enabled)
        if inttask0 and inttask1:
            return kmat[0,0]
        elif inttask0 and not inttask1:
            return kmat[0]
        elif not inttask0 and inttask1:
            return kmat[:,0]
        else: # not inttask0 and not inttask1
            return kmat
    def post_var(self, x:torch.Tensor, task:torch.Tensor=None, n:torch.Tensor=None, eval:bool=True):
        """
        Posterior variance. Forced to be non-negative.  

        Args:
            x (torch.Tensor[N,d]): sampling locations
            n (torch.Tensor): Number of points at which to evaluate the posterior cubature variance. Defaults to `n=self.n`. `n` must be powers of 2.  
            eval (bool): if `True`, disable gradients, otherwise use `torch.is_grad_enabled()`
            task (torch.Tensor): task indices

        Returns:
            pvar (torch.Tensor[N]): posterior variance vector
        """
        if n is None: n = self.n
        assert isinstance(n,torch.Tensor) and (n&(n-1)==0).all() and (n>=self.n).all(), "require n are all power of two greater than or equal to self.n"
        assert x.ndim==2 and x.size(1)==self.d, "x must a torch.Tensor with shape (-1,d)"
        kmat_tasks = self.gram_matrix_tasks
        if eval:
            incoming_grad_enabled = torch.is_grad_enabled()
            torch.set_grad_enabled(False)
        if task is None: task = torch.arange(self.num_tasks)
        inttask = isinstance(task,int)
        if inttask: task = torch.tensor([task],dtype=int)
        if isinstance(task,list): task = torch.tensor(task,dtype=int)
        assert task.ndim==1 and (task>=0).all() and (task<self.num_tasks).all()
        kmat_new = self.kernel(x,x)*kmat_tasks[task,task,None]
        kmat = torch.cat([self.kernel(x[:,None,:],self.get_xb(l,n[l])[None,:,:])*kmat_tasks[task,l,None,None] for l in range(self.num_tasks)],dim=-1)
        t = self.get_inv_log_det_cache(n).gram_matrix_solve(kmat.transpose(dim0=-2,dim1=-1)).transpose(dim0=-2,dim1=-1)
        diag = kmat_new-torch.einsum("til,til->ti",t,kmat)
        diag[diag<0] = 0 
        if eval:
            torch.set_grad_enabled(incoming_grad_enabled)
        return diag[0] if inttask else diag
    def post_ci(self, x, task:torch.Tensor=None, confidence:float=0.99, eval:bool=True):
        """
        Posterior credible interval.

        Args:
            x (torch.Tensor[N,d]): sampling locations
            confidence (float): confidence level in $(0,1)$ for the credible interval
            eval (bool): if `True`, disable gradients, otherwise use `torch.is_grad_enabled()`
            task (torch.Tensor): task indices

        Returns:
            pmean (torch.Tensor[*batch_shape,N]): posterior mean where `batch_shape` is inferred from `y=f(x)`
            pvar (torch.Tensor[N]): posterior variance vector
            quantile (np.float64):
                ```python
                scipy.stats.norm.ppf(1-(1-confidence)/2)
                ```
            ci_low (torch.Tensor[*batch_shape,N]): credible interval lower bound
            ci_high (torch.Tensor[*batch_shape,N]): credible interval upper bound
        """
        assert np.isscalar(confidence) and 0<confidence<1, "confidence must be between 0 and 1"
        if eval:
            incoming_grad_enabled = torch.is_grad_enabled()
            torch.set_grad_enabled(False)
        q = scipy.stats.norm.ppf(1-(1-confidence)/2)
        pmean = self.post_mean(x,task) 
        pvar = self.post_var(x,task)
        pstd = torch.sqrt(pvar)
        ci_low = pmean-q*pstd 
        ci_high = pmean+q*pstd
        if eval:
            torch.set_grad_enabled(incoming_grad_enabled)
        return pmean,pvar,q,ci_low,ci_high
    def post_cubature_mean(self, task:torch.Tensor=None, eval:bool=True):
        """
        Posterior cubature mean. 

        Args:
            eval (bool): if `True`, disable gradients, otherwise use `torch.is_grad_enabled()`
            task (torch.Tensor): task indices

        Returns:
            pcmean (torch.Tensor[*batch_shape]): posterior cubature mean where `batch_shape` is inferred from `y=f(x)`
        """
        kmat_tasks = self.gram_matrix_tasks
        coeffs = self.coeffs
        if eval:
            incoming_grad_enabled = torch.is_grad_enabled()
            torch.set_grad_enabled(False)
        if task is None: task = torch.arange(self.num_tasks)
        inttask = isinstance(task,int)
        if inttask: task = torch.tensor([task],dtype=int)
        if isinstance(task,list): task = torch.tensor(task,dtype=int)
        assert task.ndim==1 and (task>=0).all() and (task<self.num_tasks).all()
        coeffs_split = coeffs.split(self.n.tolist(),-1)
        coeffs_split_scaled = [self.scale*coeffs_split[l][...,None,:]*kmat_tasks[task,l,None] for l in range(self.num_tasks)]
        pcmean = torch.cat(coeffs_split_scaled,-1).sum(-1)
        if eval:
            torch.set_grad_enabled(incoming_grad_enabled)
        return pcmean[0] if inttask else pcmean
    def post_cubature_cov(self, task0:torch.Tensor=None, task1:torch.Tensor=None, n:int=None, eval:bool=True):
        """
        Posterior cubature covariance. 

        Args:
            task0 (torch.Tensor): task indices
            task1 (torch.Tensor): task indices
            n (torch.Tensor): Number of points at which to evaluate the posterior cubature variance. Defaults to `n=self.n`. `n` must be powers of 2.  
            eval (bool): if `True`, disable gradients, otherwise use `torch.is_grad_enabled()`

        Returns:
            pcvar (torch.Tensor[*batch_shape]): posterior cubature variance where `batch_shape` is inferred from `y=f(x)`
        """
        if n is None: n = self.n
        assert isinstance(n,torch.Tensor) and (n&(n-1)==0).all() and (n>=self.n).all(), "require n are all power of two greater than or equal to self.n"
        kmat_tasks = self.gram_matrix_tasks
        inv_log_det_cache = self.get_inv_log_det_cache(n)
        inv = inv_log_det_cache()[0]
        to = inv_log_det_cache.task_order
        nord = n[to]
        mvec = torch.hstack([torch.zeros(1),(nord/nord[-1]).cumsum(0)]).to(int)[:-1]
        nsqrts = torch.sqrt(nord[:,None]*nord[None,:])
        if eval:
            incoming_grad_enabled = torch.is_grad_enabled()
            torch.set_grad_enabled(False)
        if task0 is None: task0 = torch.arange(self.num_tasks)
        inttask0 = isinstance(task0,int)
        if inttask0: task0 = torch.tensor([task0],dtype=int)
        if isinstance(task0,list): task0 = torch.tensor(task0,dtype=int)
        assert task0.ndim==1 and (task0>=0).all() and (task0<self.num_tasks).all()
        if task1 is None: task1 = torch.arange(self.num_tasks)
        inttask1 = isinstance(task1,int)
        if inttask1: task1 = torch.tensor([task1],dtype=int)
        if isinstance(task1,list): task1 = torch.tensor(task1,dtype=int)
        assert task1.ndim==1 and (task1>=0).all() and (task1<self.num_tasks).all()
        inv_cut = inv[mvec,:][:,mvec][:,:,0].real
        kmat_tasks_left = kmat_tasks[task0,:][:,to].to(self._FTOUTDTYPE)
        kmat_tasks_right = kmat_tasks[to,:][:,task1].to(self._FTOUTDTYPE)
        term = torch.einsum("ij,jk,kl->il",kmat_tasks_left,nsqrts*inv_cut,kmat_tasks_right).real
        pccov = self.scale*kmat_tasks[task0,:][:,task1]-self.scale**2*term
        if eval:
            torch.set_grad_enabled(incoming_grad_enabled)
        if inttask0 and inttask1:
            return pccov[0,0]
        elif inttask0 and not inttask1:
            return pccov[0]
        elif not inttask0 and inttask1:
            return pcov[:,0]
        else: #not inttask0 and not inttask1
            return pccov
    def post_cubature_var(self, task:torch.Tensor=None, n:int=None, eval:bool=True):
        """
        Posterior cubature variance. 

        Args:
            task (torch.Tensor): task indices
            n (torch.Tensor): Number of points at which to evaluate the posterior cubature variance. Defaults to `n=self.n`. `n` must be powers of 2.  
            eval (bool): if `True`, disable gradients, otherwise use `torch.is_grad_enabled()`

        Returns:
            pcvar (torch.Tensor[*batch_shape]): posterior cubature variance where `batch_shape` is inferred from `y=f(x)`
        """
        if n is None: n = self.n
        assert isinstance(n,torch.Tensor) and (n&(n-1)==0).all() and (n>=self.n).all(), "require n are all power of two greater than or equal to self.n"
        kmat_tasks = self.gram_matrix_tasks
        inv_log_det_cache = self.get_inv_log_det_cache(n)
        inv = inv_log_det_cache()[0]
        to = inv_log_det_cache.task_order
        nord = n[to]
        mvec = torch.hstack([torch.zeros(1),(nord/nord[-1]).cumsum(0)]).to(int)[:-1]
        nsqrts = torch.sqrt(nord[:,None]*nord[None,:])
        if eval:
            incoming_grad_enabled = torch.is_grad_enabled()
            torch.set_grad_enabled(False)
        if task is None: task = torch.arange(self.num_tasks)
        inttask = isinstance(task,int)
        if inttask: task = torch.tensor([task],dtype=int)
        if isinstance(task,list): task = torch.tensor(task,dtype=int)
        assert task.ndim==1 and (task>=0).all() and (task<self.num_tasks).all()
        inv_cut = inv[mvec,:][:,mvec][:,:,0]
        kmat_tasks_left = kmat_tasks[task,:][:,to].to(self._FTOUTDTYPE)
        kmat_tasks_right = kmat_tasks[to,:][:,task].to(self._FTOUTDTYPE)
        term = torch.einsum("ij,jk,ki->i",kmat_tasks_left,nsqrts*inv_cut,kmat_tasks_right).real
        pcvar = self.scale*kmat_tasks[task,task]-self.scale**2*term
        if eval:
            torch.set_grad_enabled(incoming_grad_enabled)
        return pcvar[0] if inttask else pcvar
    def post_cubature_ci(self, confidence:float=0.99, eval:bool=True):
        """
        Posterior cubature credible.

        Args:
            confidence (float): confidence level in $(0,1)$ for the credible interval
            eval (bool): if `True`, disable gradients, otherwise use `torch.is_grad_enabled()`
        
        Returns:
            pcmean (torch.Tensor[*batch_shape]): posterior cubature mean where `batch_shape` is inferred from `y=f(x)`
            pcvar (torch.Tensor[*batch_shape]): posterior cubature variance
            quantile (np.float64):
                ```python
                scipy.stats.norm.ppf(1-(1-confidence)/2)
                ```
            cci_low (torch.Tensor[*batch_shape]): scalar credible interval lower bound
            cci_high (torch.Tensor[*batch_shape]): scalar credible interval upper bound
        """
        if eval:
            incoming_grad_enabled = torch.is_grad_enabled()
            torch.set_grad_enabled(False)
        assert np.isscalar(confidence) and 0<confidence<1, "confidence must be between 0 and 1"
        q = scipy.stats.norm.ppf(1-(1-confidence)/2)
        pmean = self.post_cubature_mean() 
        pvar = self.post_cubature_var()
        pstd = torch.sqrt(pvar)
        ci_low = pmean-q*pstd 
        ci_high = pmean+q*pstd 
        if eval:
            torch.set_grad_enabled(incoming_grad_enabled)
        return pmean,pvar,q,ci_low,ci_high
    def fit(self,
        iterations:int = 5000,
        optimizer:torch.optim.Optimizer = None,
        lr:float = 1e-1,
        store_mll_hist:bool = True, 
        store_scale_hist:bool = True, 
        store_lengthscales_hist:bool = True,
        store_noise_hist:bool = True,
        verbose:int = 1,
        verbose_indent:int = 4,
        stop_crit_improvement_threshold:float = 1e0,
        stop_crit_wait_iterations:int = 10,
        ):
        """
        Args:
            iterations (int): number of optimization iterations
            optimizer (torch.optim.Optimizer): optimizer defaulted to `torch.optim.Rprop(self.parameters(),lr=lr)`
            lr (float): learning rate for default optimizer
            store_mll_hist (bool): if `True`, store and return iteration data for mll
            store_scale_hist (bool): if `True`, store and return iteration data for the kernel scale parameter
            store_lengthscales_hist (bool): if `True`, store and return iteration data for the kernel lengthscale parameters
            store_noise_hist (bool): if `True`, store and return iteration data for noise
            verbose (int): log every `verbose` iterations, set to `0` for silent mode
            verbose_indent (int): size of the indent to be applied when logging, helpful for logging multiple models
            stop_crit_improvement_threshold (float): stop fitting when the maximum number of iterations is reached or the best mll is note reduced by `stop_crit_improvement_threshold` for `stop_crit_wait_iterations` iterations 
            stop_crit_wait_iterations (int): number of iterations to wait for improved mll before early stopping, see the argument description for `stop_crit_improvement_threshold`
        
        Returns:
            data (dict): iteration data which, dependeing on storage arguments, may include keys in 
                ```python
                ["mll_hist","scale_hist","lengthscales_hist","noise_hist"]
                ```
        """
        assert (self.n>0).any(), "cannot fit without data"
        assert isinstance(iterations,int) and iterations>=0
        if optimizer is None:
            assert np.isscalar(lr) and lr>0, "require lr is a positive float"
            optimizer = torch.optim.Rprop(self.parameters(),lr=lr)
        assert isinstance(optimizer,torch.optim.Optimizer)
        assert isinstance(store_mll_hist,bool), "require bool store_mll_hist" 
        assert isinstance(store_scale_hist,bool), "require bool store_scale_hist" 
        assert isinstance(store_lengthscales_hist,bool), "require bool store_lengthscales_hist" 
        assert isinstance(store_noise_hist,bool), "require bool store_noise_hist"
        assert (isinstance(verbose,int) or isinstance(verbose,bool)) and verbose>=0, "require verbose is a non-negative int"
        assert isinstance(verbose_indent,int) and verbose_indent>=0, "require verbose_indent is a non-negative int"
        assert isinstance(stop_crit_improvement_threshold,float) and 0<stop_crit_improvement_threshold, "require stop_crit_improvement_threshold is a positive float"
        assert isinstance(stop_crit_wait_iterations,int) and stop_crit_wait_iterations>0
        logtol = np.log(1+stop_crit_improvement_threshold)
        if store_mll_hist:
            mll_hist = torch.empty(iterations+1)
        store_scale_hist = store_scale_hist and self.raw_scale.requires_grad
        store_lengthscales_hist = store_lengthscales_hist and self.raw_lengthscales.requires_grad
        store_noise_hist = store_noise_hist and self.raw_noise.requires_grad
        if store_scale_hist:
            scale_hist = torch.empty(iterations+1)
        if store_lengthscales_hist:
            lengthscales_hist = torch.empty((iterations+1,self.d))
        if store_noise_hist:
            noise_hist = torch.empty(iterations+1)
        if verbose:
            _s = "%16s | %-10s | %-10s | %-10s | %-20s | %-s "%("iter of %.1e"%iterations,"NMLL","noise","scale","lengthscales","task_kernel")
            print(" "*verbose_indent+_s)
            print(" "*verbose_indent+"~"*len(_s))
        mll_const = self.d_out*self.n.sum()*np.log(2*np.pi)
        stop_crit_best_mll = torch.inf 
        stop_crit_save_mll = torch.inf 
        stop_crit_iterations_without_improvement_mll = 0
        ytildes = [self.get_ytilde(i) for i in range(self.num_tasks)]
        ytildescat = torch.cat(ytildes,dim=-1)
        os.environ["FASTGP_FORCE_RECOMPILE"] = "True"
        for i in range(iterations+1):
            ztildes,logdet = self.get_inv_log_det_cache()._gram_matrix_solve_tilde_to_tilde(ytildes)
            ztildescat = torch.cat(ztildes,dim=-1)
            term1 = (ytildescat.conj()*ztildescat).real.sum()
            term2 = self.d_out*logdet
            mll = term1+term2+mll_const
            if mll.item()<stop_crit_best_mll:
                stop_crit_best_mll = mll.item()
            if (stop_crit_save_mll-mll.item())>logtol:
                stop_crit_iterations_without_improvement_mll = 0
                stop_crit_save_mll = stop_crit_best_mll
            else:
                stop_crit_iterations_without_improvement_mll += 1
            break_condition = i==iterations or stop_crit_iterations_without_improvement_mll==stop_crit_wait_iterations
            if store_mll_hist:
                mll_hist[i] = mll.item()
            if store_scale_hist:
                scale_hist[i] = self.scale.item()
            if store_lengthscales_hist:
                lengthscales_hist[i] = self.lengthscales.detach().to(lengthscales_hist.device)
            if store_noise_hist:
                noise_hist[i] = self.noise.item()
            if verbose and (i%verbose==0 or break_condition):
                with np.printoptions(formatter={"float":lambda x: "%.1e"%x},threshold=6,edgeitems=3):
                    _s = "%16.2e | %-10.2e | %-10.2e | %-10.2e | %-20s | %-s"%\
                        (i,mll.item(),self.noise.item(),self.scale.item(),str(self.lengthscales.detach().cpu().numpy()),str(self.gram_matrix_tasks.detach().cpu().numpy()).replace("\n",""))
                print(" "*verbose_indent+_s)
            if break_condition: break
            mll.backward()#retain_graph=True)
            optimizer.step()
            optimizer.zero_grad()
            if self.get_inv_log_det_cache()._frozen_equal(): break
        del os.environ["FASTGP_FORCE_RECOMPILE"]
        data = {}
        if store_mll_hist:
            data["mll_hist"] = mll_hist
        if store_scale_hist:
            data["scale_hist"] = scale_hist
        if store_lengthscales_hist:
            data["lengthscales_hist"] = lengthscales_hist
        if store_noise_hist:
            data["noise_hist"] = noise_hist
        return data


