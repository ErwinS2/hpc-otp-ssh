#!/bin/bash
# Slurm 作业模板。把 <...> 换成你的实际值后：
#   hpc-ssh run 'mkdir -p ~/jobs'
#   hpc-ssh submit ./examples/job.sh --remote-dir ~/jobs
#
# 账户/分区/QOS 怎么查：
#   hpc-ssh run 'sacctmgr show ass user=$(whoami) format=account%15,partition%15,qos%15'
#   hpc-ssh run 'sinfo -o %P,%a,%l,%D,%t'

#SBATCH -o job.%j.out
#SBATCH -J myjob
#SBATCH --partition=<分区，如 C032M0128G>
#SBATCH --account=<账户名>
#SBATCH --qos=low
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=8
#SBATCH --time=02:00:00

module purge

echo "hostname : $(hostname)"
echo "jobid    : $SLURM_JOB_ID"
echo "nodelist : $SLURM_JOB_NODELIST"
echo "date     : $(date)"

# 你的计算命令写在这里，例如：
# module load intel/2018.0
# mpirun -n 8 ./my_program
