from IR import Pipe, MultiUseParameterConfig, pipe_split
import torch
import torch.fx
from typing import Dict
import operator
import logging

import os
local_rank = int(os.environ["LOCAL_RANK"])
world_size = int(os.environ["WORLD_SIZE"])

import torch.distributed.rpc as rpc

logging.getLogger().setLevel(logging.INFO)

def to_here(a):
    if isinstance(a, torch._C._distributed_rpc.PyRRef):
        return a.to_here()
    else:
        return a

class PipeStageExecutor:
    def __init__(self, mod):
        logging.info(f'Instantiating PipeStageExecutor for module {mod}')
        self.mod = mod

    def invoke(self, args, kwargs):
        args = torch.fx.node.map_aggregate(args, to_here)
        kwargs = torch.fx.node.map_aggregate(kwargs, to_here)
        logging.info(f'invoked target {self.mod} on rank {local_rank}')
        return self.mod(*args, **kwargs)

def tuple_idx(val_rref, idx):
    return val_rref.to_here()[idx]

rpc.init_rpc(f'worker{local_rank}', rank=local_rank, world_size=world_size)

if local_rank == 0:
    class ExampleCode(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.mm_param = torch.nn.Parameter(torch.randn(512, 512))
            self.mm_param2 = torch.nn.Parameter(torch.randn(512, 512))
            self.lin = torch.nn.Linear(512, 512)

        def forward(self, x):
            x = torch.mm(x, self.mm_param)
            skip_connection = x
            x = torch.relu(x)
            pipe_split()
            x = torch.mm(x, self.mm_param)
            x = self.lin(x)
            pipe_split()
            x = torch.relu(x)
            x = x + skip_connection
            x = torch.mm(x, self.mm_param2)
            x = self.lin(x)
            return x

    ec = ExampleCode()
    ec(torch.randn(50, 512))

    ec_pipe = Pipe.from_tracing(ec, MultiUseParameterConfig.TRANSMIT)

    def loss_code(x):
        return torch.sum(x)

    optimizer = torch.optim.SGD(ec_pipe.parameters(), 0.01)

    remote_stage_executor_rrefs : Dict[str, torch.distributed.rpc.RRef] = {}

    for rank, (name, mod) in enumerate(ec_pipe.split_gm.named_children()):
        remote_stage_executor_rrefs[name] = (rank, rpc.remote(rank, PipeStageExecutor, (mod,)))

    # Interpret top-level graph and issue remote calls

    class RemoteInterpreter(torch.fx.Interpreter):
        def __init__(self, remote_stage_executor_rrefs, module, garbage_collect_values = True):
            super().__init__(module, garbage_collect_values)
            self.remote_stage_executor_rrefs = remote_stage_executor_rrefs

        def call_module(self, target, args, kwargs):
            assert isinstance(target, str)

            if target in self.remote_stage_executor_rrefs:
                rank, stage_executor = self.remote_stage_executor_rrefs[target]
                logging.info(f'Issuing remote invocation for target {target} on rank {rank}')
                return stage_executor.remote().invoke(args, kwargs)
            else:
                logging.info(f'Running local operation {target} from driver')
                return super().call_module(target, args, kwargs)

        def call_function(self, target, args, kwargs):
            if target is operator.getitem and isinstance(args[0], torch._C._distributed_rpc.PyRRef):
                return rpc.remote(args[0].owner().id, tuple_idx, args)
            return super().call_function(target, args, kwargs)

    interp = RemoteInterpreter(remote_stage_executor_rrefs, ec_pipe.split_gm)

    input = torch.randn(50, 512)

    out = interp.run(input)

    ref_out = ec_pipe.split_gm(input)

    torch.testing.assert_allclose(out.to_here(), ref_out)

rpc.shutdown()

# TODOs:
#
# * Serialize execution of jobs on a single stage -- create a scheduler class
# * Implement schedules and scheduling language: fill-drain, 1f1b, interleaved, backpressure
# * Autograd
# * Loss invocations