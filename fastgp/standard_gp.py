from .abstract_gp import AbstractGP
from .util import (
    _StandardInverseLogDetCache,
)
import torch
import numpy as np
import qmcpy as qmcpy
from typing import Tuple,Union
import os

class StandardGP(AbstractGP):
    """
    Standard Gaussian process regression
    
    Examples:
        >>> torch.set_default_dtype(torch.float64)

        >>> def f_ackley(x, a=20, b=0.2, c=2*np.pi, scaling=32.768):
        ...     # https://www.sfu.ca/~ssurjano/ackley.html
        ...     assert x.ndim==2
        ...     x = 2*scaling*x-scaling
        ...     t1 = a*torch.exp(-b*torch.sqrt(torch.mean(x**2,1)))
        ...     t2 = torch.exp(torch.mean(torch.cos(c*x),1))
        ...     t3 = a+np.exp(1)
        ...     y = -t1-t2+t3
        ...     return y

        >>> n = 2**10
        >>> d = 2
        >>> sgp = StandardGP(qmcpy.DigitalNetB2(dimension=d,seed=7))
        >>> x_next = sgp.get_x_next(n)
        >>> y_next = f_ackley(x_next)
        >>> sgp.add_y_next(y_next)

        >>> rng = torch.Generator().manual_seed(17)
        >>> x = torch.rand((2**7,d),generator=rng)
        >>> y = f_ackley(x)
        
        >>> pmean = sgp.post_mean(x)

        >>> pmean.shape
        torch.Size([128])
        >>> torch.linalg.norm(y-pmean)/torch.linalg.norm(y)
        tensor(0.0336)
        >>> assert torch.allclose(sgp.post_mean(sgp.x),sgp.y)

        >>> data = sgp.fit()
             iter of 5.0e+03 | NMLL       | norm term  | logdet term
            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
                    0.00e+00 | 3.35e+03   | 1.46e+03   | 1.32e+01  
                    5.00e+00 | 3.24e+03   | 1.05e+03   | 3.11e+02  
                    1.00e+01 | 3.13e+03   | 9.55e+02   | 2.96e+02  
                    1.50e+01 | 3.10e+03   | 1.03e+03   | 1.86e+02  
                    2.00e+01 | 3.10e+03   | 1.01e+03   | 2.02e+02  
                    2.50e+01 | 3.09e+03   | 1.04e+03   | 1.71e+02  
                    3.00e+01 | 3.09e+03   | 1.03e+03   | 1.75e+02  
                    3.50e+01 | 3.09e+03   | 1.04e+03   | 1.72e+02  
                    4.00e+01 | 3.09e+03   | 1.03e+03   | 1.80e+02  
                    4.30e+01 | 3.09e+03   | 1.03e+03   | 1.84e+02  
        >>> list(data.keys())
        ['mll_hist', 'scale_hist', 'lengthscales_hist']

        >>> torch.linalg.norm(y-sgp.post_mean(x))/torch.linalg.norm(y)
        tensor(0.0355)
        >>> z = torch.rand((2**8,d),generator=rng)
        >>> pcov = sgp.post_cov(x,z)
        >>> pcov.shape
        torch.Size([128, 256])

        >>> pcov = sgp.post_cov(x,x)
        >>> pcov.shape
        torch.Size([128, 128])
        >>> assert (pcov.diagonal()>=0).all()

        >>> pvar = sgp.post_var(x)
        >>> pvar.shape
        torch.Size([128])
        >>> assert torch.allclose(pcov.diagonal(),pvar)

        >>> pmean,pstd,q,ci_low,ci_high = sgp.post_ci(x,confidence=0.99)
        >>> ci_low.shape
        torch.Size([128])
        >>> ci_high.shape
        torch.Size([128])

        >>> sgp.post_cubature_mean()
        tensor(20.1896)
        >>> sgp.post_cubature_var()
        tensor(0.0002)

        >>> pcmean,pcvar,q,pcci_low,pcci_high = sgp.post_cubature_ci(confidence=0.99)
        >>> pcci_low
        tensor(20.1564)
        >>> pcci_high
        tensor(20.2228)
        
        >>> pcov_future = sgp.post_cov(x,z,n=2*n)
        >>> pvar_future = sgp.post_var(x,n=2*n)
        >>> pcvar_future = sgp.post_cubature_var(n=2*n)
        
        >>> x_next = sgp.get_x_next(2*n)
        >>> y_next = f_ackley(x_next)
        >>> sgp.add_y_next(y_next)
        >>> torch.linalg.norm(y-sgp.post_mean(x))/torch.linalg.norm(y)
        tensor(0.0258)

        >>> assert torch.allclose(sgp.post_cov(x,z),pcov_future)
        >>> assert torch.allclose(sgp.post_var(x),pvar_future)
        >>> assert torch.allclose(sgp.post_cubature_var(),pcvar_future)

        >>> data = sgp.fit(verbose=False,store_mll_hist=False,store_scale_hist=False,store_lengthscales_hist=False,store_noise_hist=False)
        >>> assert len(data)==0
        >>> torch.linalg.norm(y-sgp.post_mean(x))/torch.linalg.norm(y)
        tensor(0.0259)

        >>> x_next = sgp.get_x_next(4*n)
        >>> y_next = f_ackley(x_next)
        >>> sgp.add_y_next(y_next)
        >>> torch.linalg.norm(y-sgp.post_mean(x))/torch.linalg.norm(y)
        tensor(0.0191)

        >>> data = sgp.fit(verbose=False,store_mll_hist=False,store_scale_hist=False,store_lengthscales_hist=False,store_noise_hist=False)
        >>> assert len(data)==0
        >>> torch.linalg.norm(y-sgp.post_mean(x))/torch.linalg.norm(y)
        tensor(0.0187)

        >>> pcov_16n = sgp.post_cov(x,z,n=16*n)
        >>> pvar_16n = sgp.post_var(x,n=16*n)
        >>> pcvar_16n = sgp.post_cubature_var(n=16*n)
        >>> x_next = sgp.get_x_next(16*n)
        >>> y_next = f_ackley(x_next)
        >>> sgp.add_y_next(y_next)
        >>> assert torch.allclose(sgp.post_cov(x,z),pcov_16n)
        >>> assert torch.allclose(sgp.post_var(x),pvar_16n)
        >>> assert torch.allclose(sgp.post_cubature_var(),pcvar_16n)
    """
    _XBDTYPE = torch.float64
    _FTOUTDTYPE = torch.float64
    def __init__(self,
            seqs:Union[qmcpy.IIDStdUniform,int],
            num_tasks:int = None,
            seed_for_seq:int = None,
            scale:float = 1., 
            lengthscales:Union[torch.Tensor,float] = 1., 
            noise:float = 1e-8,
            factor_task_kernel:Union[torch.Tensor,int] = 1.,
            rank_factor_task_kernel:int = None,
            noise_task_kernel:Union[torch.Tensor,float] = 1.,
            device:torch.device = "cpu",
            tfs_scale:Tuple[callable,callable] = ((lambda x: torch.log(x)),(lambda x: torch.exp(x))),
            tfs_lengthscales:Tuple[callable,callable] = ((lambda x: torch.log(x)),(lambda x: torch.exp(x))),
            tfs_noise:Tuple[callable,callable] = ((lambda x: torch.log(x)),(lambda x: torch.exp(x))),
            tfs_factor_task_kernel:Tuple[callable,callable] = ((lambda x: x, lambda x: x)),#((lambda x: x**(1/3)),(lambda x: x**3)),
            tfs_noise_task_kernel:Tuple[callable,callable] = ((lambda x: torch.log(x)),(lambda x: torch.exp(x))),
            requires_grad_scale:bool = True, 
            requires_grad_lengthscales:bool = True, 
            requires_grad_noise:bool = False, 
            requires_grad_factor_task_kernel:bool = None,
            requires_grad_noise_task_kernel:bool = None,
            shape_batch:torch.Size = torch.Size([]),
            shape_scale:torch.Size = torch.Size([1]), 
            shape_lengthscales:torch.Size = None,
            shape_noise:torch.Size = torch.Size([1]),
            shape_factor_task_kernel:torch.Size = None, 
            shape_noise_task_kernel:torch.Size = None,
            derivatives:list = None,
            derivatives_coeffs:list = None,
            kernel_class:str = "Gaussian"
            ):
        """
        Args:
            seqs (Union[int,qmcpy.DiscreteDistribution,List]]): list of sequence generators. If an int `d` is passed in we use 
                ```python
                [qmcpy.DigitalNetB2(d,seed=seed) for seed in np.random.SeedSequence(seed_for_seq).spawn(num_tasks)]
                ```
                See the <a href="https://qmcpy.readthedocs.io/en/latest/algorithms.html#discrete-distribution-class" target="_blank">`qmcpy.DiscreteDistribution` docs</a> for more info. 
            num_tasks (int): number of tasks 
            seed_for_seq (int): seed used for digital net randomization
            scale (float): kernel global scaling parameter
            lengthscales (Union[torch.Tensor[d],float]): vector of kernel lengthscales. 
                If a scalar is passed in then `lengthscales` is set to a constant vector. 
            noise (float): positive noise variance i.e. nugget term
            factor_task_kernel (Union[Tensor[num_tasks,rank_factor_task_kernel],int]): for $F$ the `factor_task_kernel` the task kernel is $FF^T + \\text{diag}(\\boldsymbol{v})$ 
                where `rank_factor_task_kernel<=num_tasks` and $\\boldsymbol{v}$ is the `noise_task_kernel`.
            rank_factor_task_kernel (int): see the description of `factor_task_kernel` above. Defaults to 0 for single task problems and 1 for multi task problems.
            noise_task_kernel (Union[torch.Tensor[num_tasks],float]): see the description of `factor_task_kernel` above 
            device (torch.device): torch device which is required to support `torch.float64`
            tfs_scale (Tuple[callable,callable]): the first argument transforms to the raw value to be optimized, the second applies the inverse transform
            tfs_lengthscales (Tuple[callable,callable]): the first argument transforms to the raw value to be optimized, the second applies the inverse transform
            tfs_noise (Tuple[callable,callable]): the first argument transforms to the raw value to be optimized, the second applies the inverse transform
            tfs_factor_task_kernel (Tuple[callable,callable]): the first argument transforms to the raw value to be optimized, the second applies the inverse transform
            tfs_noise_task_kernel (Tuple[callable,callable]): the first argument transforms to the raw value to be optimized, the second applies the inverse transform
            requires_grad_scale (bool): wheather or not to optimize the scale parameter
            requires_grad_lengthscales (bool): wheather or not to optimize lengthscale parameters
            requires_grad_noise (bool): wheather or not to optimize the noise parameter
            requires_grad_factor_task_kernel (bool): wheather or not to optimize the factor for the task kernel
            requires_grad_noise_task_kernel (bool): wheather or not to optimize the noise for the task kernel
            shape_batch (torch.Size): shape of the batch output for each task
            shape_scale (torch.Size): shape of the scale parameter, defaults to `torch.Size([1])`
            shape_lengthscales (torch.Size): shape of the lengthscales parameter, defaults to `torch.Size([d])` where `d` is the dimension
            shape_noise (torch.Size): shape of the noise parameter, defaults to `torch.Size([1])`
            shape_factor_task_kernel (torch.Size): shape of the factor for the task kernel, defaults to `torch.Size([num_tasks,r])` where `r` is the rank, see the description of `factor_task_kernel`
            shape_noise_task_kernel (torch.Size): shape of the noise for the task kernel, defaults to `torch.Size([num_tasks])`
            derivatives (list): list of derivative orders e.g. to include a function and its gradient set 
                ```python
                derivatives = [torch.zeros(d,dtype=int)]+[ej for ej in torch.eye(d,dtype=int)]
                ```
            derivatives_coeffs (list): list of derivative coefficients where if `derivatives[k].shape==(p,d)` then we should have `derivatives_coeffs[k].shape==(p,)`
        """
        if num_tasks is None: 
            solo_task = True
            default_task = 0 
            num_tasks = 1
        else:
            assert isinstance(num_tasks,int) and num_tasks>0
            solo_task = False
            default_task = torch.arange(num_tasks)
        if isinstance(seqs,int):
            seqs = np.array([qmcpy.DigitalNetB2(seqs,seed=seed) for seed in np.random.SeedSequence(seed_for_seq).spawn(num_tasks)],dtype=object)
        if isinstance(seqs,qmcpy.DiscreteDistribution):
            seqs = np.array([seqs],dtype=object)
        if isinstance(seqs,list):
            seqs = np.array(seqs,dtype=object)
        assert seqs.shape==(num_tasks,), "seqs should be a length num_tasks=%d list"%num_tasks
        assert all(seqs[i].replications==1 for i in range(num_tasks)) and "each seq should have only 1 replication"
        kernel_class = kernel_class.lower()
        assert kernel_class in ["gaussian"]
        if kernel_class=="gaussian":
            self._base_kernel = self._kernel_gaussian
        super().__init__(
            seqs,
            num_tasks,
            default_task,
            solo_task,
            scale,
            lengthscales,
            noise,
            factor_task_kernel,
            rank_factor_task_kernel,
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
            shape_batch,
            shape_scale, 
            shape_lengthscales,
            shape_noise,
            shape_factor_task_kernel, 
            shape_noise_task_kernel,
            derivatives,
            derivatives_coeffs,
        )
    def _kernel_gaussian(self, x, z):
        return torch.exp(-(x-z)**2/(2*self.lengthscales)).prod(-1)
    def _kernel(self, x:torch.Tensor, z:torch.Tensor, beta0:torch.Tensor, beta1: torch.Tensor, c0:torch.Tensor, c1:torch.Tensor):
        assert c0.ndim==1 and c1.ndim==1
        assert beta0.shape==(len(c0),self.d) and beta1.shape==(len(c1),self.d)
        assert x.size(-1)==self.d and z.size(-1)==self.d
        if (beta0==0).all():
            xg = x 
        else:
            xgs = [x[...,j].clone().requires_grad_(True) for j in range(self.d)]
            xg = torch.stack(xgs,dim=-1)
        if (beta1==0).all():
            zg = z 
        else:
            zgs = [z[...,j].clone().requires_grad_(True) for j in range(self.d)]
            zg = torch.stack(zgs,dim=-1)
        y = 0
        y_part = self._base_kernel(xg,zg)
        for i0 in range(len(c0)):
            for i1 in range(len(c1)):
                y_part_clone = y_part.clone()
                for j0 in range(self.d):
                    for k in range(beta0[i0,j0]):
                        y_part_clone = torch.autograd.grad(y_part_clone,xgs[j0],grad_outputs=torch.ones_like(y_part_clone),retain_graph=True)[0]
                for j1 in range(self.d):
                    for k in range(beta1[i1,j1]):
                        y_part_clone = torch.autograd.grad(y_part_clone,zgs[j1],grad_outputs=torch.ones_like(y_part_clone),retain_graph=True)[0]
                y += c0[i0]*c1[i1]*y_part_clone
        return y
    def get_default_optimizer(self, lr):
        if lr is None: lr = 1e-1
        return torch.optim.Rprop(self.parameters(),lr=lr)
    def post_cubature_mean(self, task:Union[int,torch.Tensor]=None, eval:bool=True):
        assert False, "TODO"
        """
        Posterior cubature mean. 

        Args:
            eval (bool): if `True`, disable gradients, otherwise use `torch.is_grad_enabled()`
            task (Union[int,torch.Tensor[T]]): task indices

        Returns:
            pcmean (torch.Tensor[...,T]): posterior cubature mean
        """
        kmat_tasks = self.gram_matrix_tasks
        coeffs = self.coeffs
        if eval:
            incoming_grad_enabled = torch.is_grad_enabled()
            torch.set_grad_enabled(False)
        if task is None: task = self.default_task
        inttask = isinstance(task,int)
        if inttask: task = torch.tensor([task],dtype=int)
        if isinstance(task,list): task = torch.tensor(task,dtype=int)
        assert task.ndim==1 and (task>=0).all() and (task<self.num_tasks).all()
        coeffs_split = coeffs.split(self.n.tolist(),-1)
        coeffs_split_scaled = [(self.scale*coeffs_split[l])[...,None,:]*kmat_tasks[...,task,l,None] for l in range(self.num_tasks)]
        pcmean = torch.cat(coeffs_split_scaled,-1).sum(-1)
        if eval:
            torch.set_grad_enabled(incoming_grad_enabled)
        return pcmean[...,0] if inttask else pcmean
    def post_cubature_var(self, task:Union[int,torch.Tensor]=None, n:Union[int,torch.Tensor]=None, eval:bool=True):
        assert False, "TODO"
        """
        Posterior cubature variance. 

        Args:
            task (Union[int,torch.Tensor[T]]): task indices
            n (Union[int,torch.Tensor[num_tasks]]): number of points at which to evaluate the posterior cubature variance.
            eval (bool): if `True`, disable gradients, otherwise use `torch.is_grad_enabled()`

        Returns:
            pcvar (torch.Tensor[T]): posterior cubature variance
        """
        if n is None: n = self.n
        if isinstance(n,int): n = torch.tensor([n],dtype=int,device=self.device)
        assert isinstance(n,torch.Tensor) and (n&(n-1)==0).all() and (n>=self.n).all(), "require n are all power of two greater than or equal to self.n"
        kmat_tasks = self.gram_matrix_tasks
        inv_log_det_cache = self.get_inv_log_det_cache(n)
        inv = inv_log_det_cache()[0]
        to = inv_log_det_cache.task_order
        nord = n[to]
        mvec = torch.hstack([torch.zeros(1,device=self.device),(nord/nord[-1]).cumsum(0)]).to(int)[:-1]
        nsqrts = torch.sqrt(nord[:,None]*nord[None,:])
        if eval:
            incoming_grad_enabled = torch.is_grad_enabled()
            torch.set_grad_enabled(False)
        if task is None: task = self.default_task
        inttask = isinstance(task,int)
        if inttask: task = torch.tensor([task],dtype=int)
        if isinstance(task,list): task = torch.tensor(task,dtype=int)
        assert task.ndim==1 and (task>=0).all() and (task<self.num_tasks).all()
        inv_cut = inv[...,mvec,:,:][...,:,mvec,:][...,0]
        kmat_tasks_left = kmat_tasks[...,task,:][...,:,to].to(self._FTOUTDTYPE)
        kmat_tasks_right = kmat_tasks[...,to,:][...,:,task].to(self._FTOUTDTYPE)
        term = torch.einsum("...ij,...jk,...ki->...i",kmat_tasks_left,nsqrts*inv_cut,kmat_tasks_right).real
        pcvar = self.scale*kmat_tasks[...,task,task]-self.scale**2*term
        pcvar[pcvar<0] = 0.
        if eval:
            torch.set_grad_enabled(incoming_grad_enabled)
        return pcvar[...,0] if inttask else pcvar
    def post_cubature_cov(self, task0:Union[int,torch.Tensor]=None, task1:Union[int,torch.Tensor]=None, n:Union[int,torch.Tensor]=None, eval:bool=True):
        assert False, "TODO"
        """
        Posterior cubature covariance. 

        Args:
            task0 (Union[int,torch.Tensor[T1]]): task indices
            task1 (Union[int,torch.Tensor[T2]]): task indices
            n (Union[int,torch.Tensor[num_tasks]]): number of points at which to evaluate the posterior cubature covariance.
            eval (bool): if `True`, disable gradients, otherwise use `torch.is_grad_enabled()`

        Returns:
            pcvar (torch.Tensor[T1,T2]): posterior cubature covariance
        """
        if n is None: n = self.n
        if isinstance(n,int): n = torch.tensor([n],dtype=int,device=self.device)
        assert isinstance(n,torch.Tensor) and (n&(n-1)==0).all() and (n>=self.n).all(), "require n are all power of two greater than or equal to self.n"
        kmat_tasks = self.gram_matrix_tasks
        inv_log_det_cache = self.get_inv_log_det_cache(n)
        inv = inv_log_det_cache()[0]
        to = inv_log_det_cache.task_order
        nord = n[to]
        mvec = torch.hstack([torch.zeros(1,device=self.device),(nord/nord[-1]).cumsum(0)]).to(int)[:-1]
        nsqrts = torch.sqrt(nord[:,None]*nord[None,:])
        if eval:
            incoming_grad_enabled = torch.is_grad_enabled()
            torch.set_grad_enabled(False)
        if task0 is None: task0 = self.default_task
        inttask0 = isinstance(task0,int)
        if inttask0: task0 = torch.tensor([task0],dtype=int)
        if isinstance(task0,list): task0 = torch.tensor(task0,dtype=int)
        assert task0.ndim==1 and (task0>=0).all() and (task0<self.num_tasks).all()
        if task1 is None: task1 = self.default_task
        inttask1 = isinstance(task1,int)
        if inttask1: task1 = torch.tensor([task1],dtype=int)
        if isinstance(task1,list): task1 = torch.tensor(task1,dtype=int)
        assert task1.ndim==1 and (task1>=0).all() and (task1<self.num_tasks).all()
        equal = torch.equal(task0,task1)
        inv_cut = inv[...,mvec,:,:][...,:,mvec,:][...,0]
        kmat_tasks_left = kmat_tasks[...,task0,:][...,:,to].to(self._FTOUTDTYPE)
        kmat_tasks_right = kmat_tasks[...,to,:][...,:,task1].to(self._FTOUTDTYPE)
        term = torch.einsum("...ij,...jk,...kl->...il",kmat_tasks_left,nsqrts*inv_cut,kmat_tasks_right).real
        pccov = self.scale[...,None]*kmat_tasks[...,task0,:][...,:,task1]-self.scale[...,None]**2*term
        if equal:
            tvec = torch.arange(pccov.size(-1))
            diag = pccov[...,tvec,tvec]
            diag[diag<0] = 0. 
            pccov[...,tvec,tvec] = diag
        if eval:
            torch.set_grad_enabled(incoming_grad_enabled)
        if inttask0 and inttask1:
            return pccov[...,0,0]
        elif inttask0 and not inttask1:
            return pccov[...,0,:]
        elif not inttask0 and inttask1:
            return pccov[...,:,0]
        else: #not inttask0 and not inttask1
            return pccov
    def get_inv_log_det_cache(self, n=None):
        if n is None: n = self.n
        assert isinstance(n,torch.Tensor) and n.shape==(self.num_tasks,) and (n>=self.n).all()
        ntup = tuple(n.tolist())
        if ntup not in self.inv_log_det_cache_dict.keys():
            self.inv_log_det_cache_dict[ntup] = _StandardInverseLogDetCache(self,n)
        return self.inv_log_det_cache_dict[ntup]
    