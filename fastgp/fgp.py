import torch 
import qmcpy as qp
import numpy as np 
import scipy.stats 

class _FastGP(torch.nn.Module):
    def __init__(self,
        f,
        dd_obj,
        n,
        alpha,
        scale,
        lengthscales,
        noise,
        device,
        save_y,
        tfs_scale,
        tfs_lengthscales,
        tfs_noise,
        requires_grad_scale, 
        requires_grad_lengthscales, 
        requires_grad_noise, 
        ft,
        ift,
    ):
        super().__init__()
        assert torch.get_default_dtype()==torch.float64, "fast transforms do not work without torch.float64 precision" 
        self.device = torch.device(device)
        assert callable(f), "f must be a callable"
        self.f = f 
        self.dd_obj = dd_obj
        self.d = self.dd_obj.d
        assert np.isscalar(n) and n%1==0 and n>0 and np.log2(n)%1==0, "require n=2^m for some m>=0" 
        self.n_min = 0
        self.n_max = int(n)
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
        self.tf_noise = tfs_noise[1]
        self.raw_noise = torch.nn.Parameter(tfs_noise[0](noise),requires_grad=requires_grad_noise)
        self.ft = ft 
        self.ift = ift
        self.save_y = save_y
        self.x,self._x = self._sample(self.n_min,self.n_max)
        y = self.f(self.x)
        assert y.size(-1)==self.n_max
        self.d_out = y.numel()/self.n_max
        self.ytilde = self.ft(y)
        if self.save_y: 
            self.y = y
        self.k1full = self._kernel_parts(self._x,self._x[None,0,:])
        k1 = self._kernel_from_parts(self.k1full)
        k1[0] += self.noise
        self.lam = np.sqrt(self.n_max)*self.ft(k1)
        self.coeffs = self.ift(self.ytilde/self.lam).real
    @property
    def scale(self):
        return self.tf_scale(self.raw_scale)
    @property
    def lengthscales(self):
        return self.tf_lengthscales(self.raw_lengthscales)
    @property
    def noise(self):
        return self.tf_noise(self.raw_noise)
    def _kernel_parts(self, x, z):
        return self._kernel_parts_from_delta(self._ominus(x,z))
    def _kernel_from_parts(self, parts):
        return self.scale*(1+self.lengthscales*parts).prod(-1)
    def _kernel_from_delta(self, delta):
        return self._kernel_from_parts(self._kernel_parts_from_delta(delta))
    def kernel(self, x, z):
        return self._kernel_from_parts(self._kernel_parts(x,z))
    def post_mean_grad(self, x):
        assert x.ndim==2 and x.size(1)==self.d, "x must a torch.Tensor with shape (-1,d)"
        k = self.kernel(x[:,None,:],self._x[None,:,:])
        return torch.einsum("il,...l->...i",k,self.coeffs)
    def post_mean(self, x):
        with torch.no_grad():
            return self.post_mean_grad(x)
    def post_cov_grad(self, x, z):
        assert x.ndim==2 and x.size(1)==self.d, "x must a torch.Tensor with shape (-1,d)"
        assert z.ndim==2 and z.size(1)==self.d, "z must a torch.Tensor with shape (-1,d)"
        equal = torch.equal(x,z)
        k = self.kernel(x[:,None,:],z[None,:,:])
        k1t = self.ft(self.kernel(x[:,None,:],self._x[None,:,:]))
        k2t = k1t if equal else self.ft(self.kernel(z[:,None,:],self._x[None,:,:])) 
        kmat = k-torch.einsum("il,rl->ir",k1t.conj(),k2t/self.lam).real
        if equal:
            nrange = torch.arange(x.size(0),device=x.device)
            diag = kmat[nrange,nrange]
            diag[diag<0] = 0 
            kmat[nrange,nrange] = diag 
        return kmat
    def post_cov(self, x, z):
        with torch.no_grad():
            return self.post_cov_grad(x,z)
    def post_var_grad(self, x):
        assert x.ndim==2 and x.size(1)==self.d, "x must a torch.Tensor with shape (-1,d)"
        k = self.kernel(x,x)
        k1t = self.ft(self.kernel(x[:,None,:],self._x[None,:,:]))
        diag = k-torch.einsum("il,il->i",k1t.conj(),k1t/self.lam).real
        diag[diag<0] = 0 
        return diag        
    def post_var(self, x):
        with torch.no_grad():
            return self.post_var_grad(x)
    def post_std_grad(self, x):
        return torch.sqrt(self.post_var_grad(x))
    def post_std(self, x):
        return torch.sqrt(self.post_var(x))
    def post_ci_grad(self, x, confidence=0.99):
        assert np.isscalar(confidence) and 0<confidence<1, "confidence must be between 0 and 1"
        q = scipy.stats.norm.ppf(1-(1-confidence)/2)
        pmean = self.post_mean_grad(x) 
        pstd = self.post_std_grad(x)
        ci_low = pmean-q*pstd 
        ci_high = pmean+q*pstd 
        return pmean,pstd,q,ci_low,ci_high
    def post_ci(self, x, confidence=0.99):
        with torch.no_grad():
            return self.post_ci_grad(x)
    def fit(self,
        steps:int = 5000,
        optimizer:torch.optim.Optimizer = None,
        lr:float = 1e-1,
        store_mll_hist:bool = True, 
        store_scale_hist:bool = True, 
        store_lengthscales_hist:bool = True,
        store_noise_hist:bool = True,
        verbose:int = 5,
        verbose_indent:int = 4,
        stop_crit_improvement_threshold:float = 1e-5,
        stop_crit_wait_steps:int = 10,
    ):
        assert isinstance(steps,int) and steps>=0
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
        assert isinstance(stop_crit_improvement_threshold,float) and 0<=stop_crit_improvement_threshold<1, "require stop_crit_improvement_threshold is a float in [0,1)"
        assert isinstance(stop_crit_wait_steps,int) and stop_crit_wait_steps>0
        if store_mll_hist:
            mll_hist = torch.empty(steps+1)
        store_scale_hist = store_scale_hist and self.raw_scale.requires_grad
        store_lengthscales_hist = store_lengthscales_hist and self.raw_lengthscales.requires_grad
        store_noise_hist = store_noise_hist and self.raw_noise.requires_grad
        if store_scale_hist:
            scale_hist = torch.empty(steps+1)
        if store_lengthscales_hist:
            lengthscales_hist = torch.empty((steps+1,self.d))
        if store_noise_hist:
            noise_hist = torch.empty(steps+1)
        if verbose:
            _s = "%16s | %-10s | %-10s | %-10s | %-s"%("step of %.1e"%steps,"NMLL","noise","scale","lengthscales")
            print(" "*verbose_indent+_s)
            print(" "*verbose_indent+"~"*len(_s))
        mll_const = self.d_out*self.n_max*np.log(2*np.pi)
        stop_crit_best_mll = torch.inf 
        stop_crit_save_mll = torch.inf 
        stop_crit_steps_without_improvement_mll = 0
        for i in range(steps+1):
            mll = (torch.abs(self.ytilde)**2/self.lam.real).sum()+self.d_out*torch.log(torch.abs(self.lam)).sum()+mll_const
            if mll.item()<stop_crit_best_mll:
                stop_crit_best_mll = mll.item()
            if mll.item()<stop_crit_save_mll*(1-stop_crit_improvement_threshold):
                stop_crit_steps_without_improvement_mll = 0
                stop_crit_save_mll = stop_crit_best_mll
            else:
                stop_crit_steps_without_improvement_mll += 1
            break_condition = i==steps or stop_crit_steps_without_improvement_mll==stop_crit_wait_steps
            if store_mll_hist:
                mll_hist[i] = mll.item()
            if store_scale_hist:
                scale_hist[i] = self.scale.item()
            if store_lengthscales_hist:
                lengthscales_hist[i] = self.lengthscales.detach().to(lengthscales_hist.device)
            if store_noise_hist:
                noise_hist[i] = self.noise.item()
            if verbose and (i%verbose==0 or break_condition):
                with np.printoptions(formatter={"float":lambda x: "%.2e"%x},threshold=6,edgeitems=3):
                    _s = "%16.2e | %-10.2e | %-10.2e | %-10.2e | %-s"%\
                        (i,mll.item(),self.noise.item(),self.scale.item(),str(self.lengthscales.detach().cpu().numpy()))
                print(" "*verbose_indent+_s)
            if break_condition: break
            mll.backward()
            optimizer.step()
            optimizer.zero_grad()
            k1 = self._kernel_from_parts(self.k1full)
            k1[0] += self.noise
            self.lam = np.sqrt(self.n_max)*self.ft(k1)
        self.coeffs = self.ift(self.ytilde/self.lam).real

class FastGPRLattice(_FastGP):
    """
    x = torch.rand(5,fgp.d)
    z = torch.rand(7,fgp.d)


    pmean = fgp.post_mean(x)
    print(pmean.shape)

    pcov = fgp.post_cov(x,z)
    print(pcov.shape)

    pcov = fgp.post_cov(fgp.x[:5],fgp.x[:5])
    print(pcov.shape)

    pvar = fgp.post_var(x[:5])
    print(pvar.shape)

    pvar = fgp.post_var(fgp.x[:5])
    print(pvar)
    print(pvar.shape)

    pmean,pstd,q,ci_low,ci_high = fgp.post_ci(fgp.x[:5])
    print(pmean)
    print(pstd)
    print(q)
    print(ci_low)
    print(ci_high)
    print(fgp.y[:5])
    """
    def __init__(self,
            f:callable = lambda x: 1/2*((10*x-5)**4-16*(10*x-5)**2+5*(10*x-5)).sum(1), # https://www.sfu.ca/~ssurjano/stybtang.html
            lattice:qp.Lattice = qp.Lattice(2,seed=7),
            n:int = 2**16,
            alpha:int = 2,
            scale:float = 1., 
            lengthscales:torch.Tensor = 1e4, 
            noise:float = 1e-16, 
            device:torch.device = "cpu",
            save_y = True,
            tfs_scale = ((lambda x: torch.log(x)),(lambda x: torch.exp(x))),
            tfs_lengthscales = ((lambda x: torch.log(x)),(lambda x: torch.exp(x))),
            tfs_noise = ((lambda x: torch.log(x)),(lambda x: torch.exp(x))),
            requires_grad_scale = True, 
            requires_grad_lengthscales = True, 
            requires_grad_noise = False, 
            compile_fts:bool = False,
            ):
        assert isinstance(alpha,int) and alpha in qp.kernel_methods.util.shift_invar_ops.BERNOULLIPOLYSDICT.keys(), "alpha must be in %s"%list(qp.kernel_methods.util.shift_invar_ops.BERNOULLIPOLYSDICT.keys())
        assert isinstance(lattice,qp.Lattice) and lattice.order=="NATURAL" and lattice.replications==1, "lattice should be a qp.Lattice instance with order='NATURAL' and replications=1"
        ft = torch.compile(qp.fftbr_torch) if compile_fts else qp.fftbr_torch
        ift = torch.compile(qp.ifftbr_torch) if compile_fts else qp.ifftbr_torch
        self.__const_for_kernel = None
        super().__init__(
            f,
            lattice,
            n,
            alpha,
            scale,
            lengthscales,
            noise,device,
            save_y,
            tfs_scale,
            tfs_lengthscales,
            tfs_noise,
            requires_grad_scale,
            requires_grad_lengthscales,
            requires_grad_noise,
            ft,
            ift,
        )
    def _sample(self, n_min, n_max):
        x = torch.from_numpy(self.dd_obj.gen_samples(n_min=n_min,n_max=n_max)).to(torch.get_default_dtype()).to(self.device)
        return x,x
    @property
    def const_for_kernel(self):
        if self.__const_for_kernel is None:
            self.__const_for_kernel = (-1)**(self.alpha+1)*torch.exp(2*self.alpha*np.log(2*np.pi)-torch.lgamma(2*self.alpha+1))
        return self.__const_for_kernel
    def _ominus(self, x, z):
        assert ((0<=x)&(x<=1)).all(), "x should have all elements in [0,1]"
        assert ((0<=z)&(z<=1)).all(), "z should have all elements in [0,1]"
        return (x-z)%1
    def _kernel_parts_from_delta(self, delta):
        return self.const_for_kernel*torch.stack([qp.kernel_methods.bernoulli_poly(2*self.alpha[j].item(),delta[...,j]) for j in range(self.d)],-1)

class FastGPRDigitalNetB2(_FastGP):
    def __init__(self,
            f:callable = lambda x: 1/2*((10*x-5)**4-16*(10*x-5)**2+5*(10*x-5)).sum(1), # https://www.sfu.ca/~ssurjano/stybtang.html
            dnb2:qp.DigitalNetB2 = qp.DigitalNetB2(2,seed=7),
            n:int = 2**16,
            alpha:int = 2,
            scale:float = 1., 
            lengthscales:torch.Tensor = 5e2, 
            noise:float = 1e-16, 
            device:torch.device = "cpu",
            save_y = True,
            tfs_scale = ((lambda x: torch.log(x)),(lambda x: torch.exp(x))),
            tfs_lengthscales = ((lambda x: torch.log(x)),(lambda x: torch.exp(x))),
            tfs_noise = ((lambda x: torch.log(x)),(lambda x: torch.exp(x))),
            requires_grad_scale = True, 
            requires_grad_lengthscales = True, 
            requires_grad_noise = False, 
            compile_fts:bool = False,
            ):
        assert isinstance(alpha,int) and alpha in qp.kernel_methods.util.dig_shift_invar_ops.WEIGHTEDWALSHFUNCSPOS.keys(), "alpha must be in %s"%list(qp.kernel_methods.util.dig_shift_invar_ops.WEIGHTEDWALSHFUNCSPOS.keys())
        assert isinstance(dnb2,qp.DigitalNetB2) and dnb2.order=="NATURAL" and dnb2.replications==1 and dnb2.t_lms<64 and dnb2.randomize in ['LMS_DS','DS','LMS','FALSE'], "dnb2 should be a qp.DigitalNetB2 instance with order='NATURAL', replications=1, t_lms<64, and randomize in ['LMS_DS','DS','LMS','FALSE']"
        self.t = dnb2.t_lms
        ft = ift = torch.compile(qp.fwht_torch) if compile_fts else qp.fwht_torch
        super().__init__(
            f,
            dnb2,
            n,
            alpha,
            scale,
            lengthscales,
            noise,device,
            save_y,
            tfs_scale,
            tfs_lengthscales,
            tfs_noise,
            requires_grad_scale,
            requires_grad_lengthscales,
            requires_grad_noise,
            ft,
            ift,
        )
    def _sample(self, n_min, n_max):
        _x = torch.from_numpy(self.dd_obj.gen_samples(n_min=n_min,n_max=n_max,return_binary=True).astype(np.int64)).to(self.device)
        x = self._convert_from_b(_x)
        return x,_x
    def _convert_to_b(self, x):
        return torch.floor((x%1)*2**(self.t)).to(torch.int64)
    def _convert_from_b(self, xb):
        return xb*2**(-self.t)
    def _ominus(self, x_or_xb, z_or_zb):
        fp_x = torch.is_floating_point(x_or_xb)
        fp_z = torch.is_floating_point(z_or_zb)
        if fp_x:
            assert ((0<=x_or_xb)&(x_or_xb<=1)).all(), "x should have all elements in [0,1]"
        if fp_z:
            assert ((0<=z_or_zb)&(z_or_zb<=1)).all(), "z should have all elements in [0,1]"
        if (not fp_x) and (not fp_z):
            return x_or_xb^z_or_zb
        elif (not fp_x) and fp_z:
            return x_or_xb^self._convert_to_b(z_or_zb)
        elif fp_x and (not fp_z):
            return self._convert_to_b(x_or_xb)^z_or_zb
        else: # fp_x and fp_z
            return self._convert_to_b(x_or_xb)^self._convert_to_b(z_or_zb)
    def _kernel_parts_from_delta(self, delta):
        return torch.stack([qp.kernel_methods.weighted_walsh_funcs(self.alpha[j].item(),delta[...,j],self.t)-1 for j in range(self.d)],-1)
