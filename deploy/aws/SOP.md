# AWS EC2 部署 SOP — SimWorld Studio

> 这是给 **本地 Claude Code** 看的 SOP。把这份文件丢给它，它应该能按 Phase 0 → 6 自动执行（aws cli 命令在本地跑，ssh 命令进 EC2 跑）。
>
> 人类用户需要做的只有 4 件事：
> 1. 提供 AWS 凭证（`aws configure`）
> 2. 提供一个域名（也可以暂时跳过 TLS，先用 IP 直连）
> 3. 在 GitHub 上加 deploy key（Phase 2 会让你贴公钥）
> 4. Claude OAuth 时用浏览器登录一次（Phase 5）

---

## Phase 0 — 前置检查（在你本地）

```bash
# 0.1 验证 aws cli 已装、已配置
aws sts get-caller-identity                # 应返回你的账号信息

# 0.2 验证 GPU 实例配额（关键！新账号默认是 0）
aws service-quotas get-service-quota \
  --service-code ec2 \
  --quota-code L-DB2E81BA \
  --region us-east-1 \
  --query 'Quota.Value' --output text
# 期望: ≥ 16 (g5.4xlarge 用 16 vCPU)
#   如果想升 g5.12xlarge 后续要 48
# 如果是 0 或不够 → 走 https://console.aws.amazon.com/servicequotas
#   申请 "Running On-Demand G and VT instances" 提到 16
#   通常 1-2 个工作日批准。批准前后面步骤都跑不了。

# 0.3 验证 SSH key 在本地
ls ~/.ssh/id_ed25519 2>/dev/null || ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
```

**配置参数（环境变量，整份 SOP 都用这些）：**

```bash
export REGION=us-east-1                    # 改成你想要的区，us-west-2 离 UCSD 近
export AZ=us-east-1a
export INSTANCE_TYPE=g5.4xlarge            # 1× A10G, 16 vCPU, 64 GB RAM (3 并发 slot)
export EBS_SIZE=500                        # GB (够装 UE + Content + 3 slot 运行时)
export KEY_NAME=simworld-aws
export SG_NAME=simworld-sg
export NAME_TAG=simworld-studio
export DOMAIN=simworld.your-lab.edu        # 改成你的域名；没有就用 EC2 公网 IP
```

**机型挑选参考：**

| 机型 | GPU | vCPU | RAM | slot 数 | $/h | 适合 |
|---|---|---|---|---|---|---|
| g5.2xlarge | 1× A10G | 8 | 32 GB | 2 | $1.21 | 最省，偶尔排队 |
| **g5.4xlarge** ⭐ | 1× A10G | 16 | 64 GB | 3 | $1.62 | **推荐：5 人 2-3 并发** |
| g5.8xlarge | 1× A10G | 32 | 128 GB | 4 | $2.45 | 4 并发，单 GPU |
| g5.12xlarge | 4× A10G | 48 | 192 GB | 4 | $5.67 | 1 GPU/slot，重负载 |

机型改了之后，记得同步改 `/etc/default/simworld` 的 `UE_POOL_SIZE` (上表"slot 数"列) 和 `UE_GPU_COUNT` (g5.12xlarge 设 4，其他都 1)。

---

## Phase 1 — 启动 EC2 实例（本地）

```bash
# 1.1 创建 SSH key pair（如果还没有）
aws ec2 describe-key-pairs --key-names "$KEY_NAME" --region "$REGION" 2>/dev/null \
  || aws ec2 import-key-pair \
       --key-name "$KEY_NAME" \
       --public-key-material fileb://$HOME/.ssh/id_ed25519.pub \
       --region "$REGION"

# 1.2 创建 Security Group
SG_ID=$(aws ec2 create-security-group \
  --group-name "$SG_NAME" \
  --description "SimWorld Studio multi-tenant deploy" \
  --region "$REGION" \
  --query 'GroupId' --output text 2>/dev/null \
  || aws ec2 describe-security-groups \
       --group-names "$SG_NAME" --region "$REGION" \
       --query 'SecurityGroups[0].GroupId' --output text)
echo "SG: $SG_ID"

MY_IP=$(curl -s https://checkip.amazonaws.com)/32

# 入站规则（重复跑会 warn duplicate，忽略）
for spec in \
  "tcp 22 $MY_IP" \
  "tcp 80 0.0.0.0/0" \
  "tcp 443 0.0.0.0/0" \
  "udp 3478 0.0.0.0/0" ; do
  read -r proto port cidr <<<"$spec"
  aws ec2 authorize-security-group-ingress \
    --group-id "$SG_ID" --protocol $proto --port $port --cidr $cidr \
    --region "$REGION" 2>/dev/null || true
done
# TURN relay 范围
aws ec2 authorize-security-group-ingress \
  --group-id "$SG_ID" --protocol udp --port 49152-65535 --cidr 0.0.0.0/0 \
  --region "$REGION" 2>/dev/null || true

# 1.3 查最新 Ubuntu 22.04 LTS x86_64 AMI
AMI_ID=$(aws ec2 describe-images --owners 099720109477 \
  --filters 'Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*' \
            'Name=state,Values=available' \
  --query 'sort_by(Images, &CreationDate)[-1].ImageId' --output text \
  --region "$REGION")
echo "AMI: $AMI_ID"

# 1.4 启动实例
INSTANCE_ID=$(aws ec2 run-instances \
  --image-id "$AMI_ID" \
  --instance-type "$INSTANCE_TYPE" \
  --key-name "$KEY_NAME" \
  --security-group-ids "$SG_ID" \
  --placement "AvailabilityZone=$AZ" \
  --block-device-mappings "[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":$EBS_SIZE,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME_TAG}]" \
  --region "$REGION" \
  --query 'Instances[0].InstanceId' --output text)
echo "Instance: $INSTANCE_ID"

# 1.5 等启动 + 拿公网 IP
aws ec2 wait instance-running --instance-ids "$INSTANCE_ID" --region "$REGION"
EC2_IP=$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --region "$REGION" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
echo "EC2_IP=$EC2_IP"

# 1.6 等 SSH 起来（最多 3 分钟）
until ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 \
       -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP 'echo ok' 2>/dev/null; do
  sleep 10
done
echo "SSH ready: ubuntu@$EC2_IP"
```

**成本：g5.4xlarge on-demand ≈ $1.62/h ≈ $585/月（12h/天）。** 不用时记得 `aws ec2 stop-instances` —— 停机只收 EBS 费（500 GB gp3 ≈ $40/月）。后面 Phase 7 会装定时停机。

---

## Phase 2 — EC2 上建 GitHub deploy key（远端，1 分钟）

```bash
# 2.1 在 EC2 上生成 ed25519 key
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP '
  ssh-keygen -t ed25519 -C "ec2-simworld-$(hostname)" -f ~/.ssh/id_ed25519 -N ""
  cat ~/.ssh/id_ed25519.pub
'
```

**⚠ 人工步骤**：把上面打印的公钥贴到
- GitHub → `SimWorld-AI/SimWorld-Studio-Internal` → **Settings** → **Deploy keys** → **Add deploy key**
- Title: `EC2 simworld-studio`
- Key: 粘贴
- ☐ **Allow write access** —— **不要勾**（read-only 就够）

```bash
# 2.2 验证能拉私有 repo
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP '
  ssh -o StrictHostKeyChecking=no -T git@github.com 2>&1 | head -3
'
# 期望: "Hi SimWorld-AI/SimWorld-Studio-Internal! You have successfully authenticated..."
```

---

## Phase 3 — Clone + Bootstrap（远端）

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP bash <<'REMOTE'
set -euo pipefail
sudo git clone -b aws git@github.com:SimWorld-AI/SimWorld-Studio-Internal.git /opt/simworld-studio
sudo /opt/simworld-studio/deploy/aws/scripts/bootstrap.sh
REMOTE
```

Bootstrap 会装 NVIDIA driver、Node、依赖，建 `simworld` 用户和目录。**驱动装完会要求重启**：

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP 'sudo reboot' || true
# 等机器回来
sleep 60
until ssh -o ConnectTimeout=5 -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP 'nvidia-smi >/dev/null' 2>/dev/null; do
  sleep 10
done
echo "GPU OK after reboot"
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP 'nvidia-smi -L'
# 期望: 4 块 A10G
```

---

## Phase 4 — 推送 UE 引擎 + 项目（本地 → EC2）

这是流量大头。**UE 引擎 58 GB，预计 30-60 分钟**（取决于上行带宽）。建议用 `tmux` 防断线。

```bash
# 4.1 项目骨架（302 MB，1-2 分钟）
cd /home/koe/SimWorld-Studio-Internal
./deploy/aws/scripts/stage-project-to-aws.sh \
  -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP

# 4.2 UE 引擎（58 GB，慢）
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP 'sudo install -d -o simworld -g simworld /opt/ue-engine'
rsync -avh --progress \
  -e "ssh -i ~/.ssh/id_ed25519" \
  --rsync-path="sudo rsync" \
  /data/koe/Linux_Unreal_Engine_5.3.2/ \
  ubuntu@$EC2_IP:/opt/ue-engine/

# 4.3 启动 Content 下载（从 HF，后台跑）
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP 'sudo systemctl start simworld-content-init'
# 监控：
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP 'journalctl -u simworld-content-init -f' &
```

Content 下完前可以继续 Phase 5，不互相阻塞。

---

## Phase 5 — Claude OAuth + Auth + TLS（远端 + 浏览器）

### 5.1 Claude OAuth（一次性，整组共用）

```bash
ssh -t -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP \
  'sudo -u simworld HOME=/var/lib/simworld/claude-home claude'
```

⚠ **人工步骤**：终端会显示 device code 和一个 URL。**用本地浏览器**打开 URL，登录 Anthropic 账号（用 lab 共享的那个），输 code。完成后 token 落在 EC2 的 `/var/lib/simworld/claude-home/.claude/`。

### 5.2 Basic Auth 用户

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP bash <<'REMOTE'
set -e
sudo htpasswd -cb /etc/nginx/htpasswd alice 'CHANGE_ME_alice_pwd'
sudo htpasswd -b  /etc/nginx/htpasswd bob   'CHANGE_ME_bob_pwd'
sudo htpasswd -b  /etc/nginx/htpasswd carol 'CHANGE_ME_carol_pwd'
# 加更多 lab 成员...
REMOTE
```

### 5.3 DNS + TLS（如果有域名）

```bash
# 把你的域名 A 记录指向 $EC2_IP
echo "在你的 DNS provider 把 $DOMAIN A 记录指到 $EC2_IP"
read -p "Done? 按 Enter 继续 "

# 改 nginx.conf 里的 server_name
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP "
  sudo sed -i 's/simworld.your-lab.edu/$DOMAIN/g' /etc/nginx/sites-available/simworld
  sudo ln -sf /etc/nginx/sites-available/simworld /etc/nginx/sites-enabled/
  sudo rm -f /etc/nginx/sites-enabled/default
  sudo nginx -t
"

# 签证书
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP \
  "sudo certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m you@your-lab.edu"
```

**没有域名的临时方案**：跳过 5.3，nginx 用 HTTP only（去掉 ssl 行），用 `http://$EC2_IP` 访问。WebRTC 在非 HTTPS 下浏览器可能拒绝麦克风等，但 Pixel Streaming 只需要 video，所以基本可用。

### 5.4 改 coturn 配置

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP "
  sudo sed -i 's/CHANGE_ME_TO_A_STRONG_SECRET/$(openssl rand -hex 24)/' /etc/turnserver.conf
  sudo sed -i 's/simworld.your-lab.edu/$DOMAIN/g' /etc/turnserver.conf
  sudo sed -i 's|# external-ip=PUBLIC_IP|external-ip=$EC2_IP|' /etc/turnserver.conf
"
```

---

## Phase 6 — 启动服务 + 验证（远端）

```bash
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP bash <<'REMOTE'
set -e

# 6.1 等 Content 下完
while [ ! -f /opt/simworld-content/.downloaded ]; do
  echo "[wait] Content still downloading..."; sleep 30
done
echo "Content ready"

# 6.2 烟测 slot pool（不真启 UE）
node /opt/simworld-studio/deploy/aws/scripts/test-slot-pool.js

# 6.3 启动服务
sudo systemctl daemon-reload
sudo systemctl enable --now coturn
sudo systemctl enable --now nginx
sudo systemctl enable --now simworld-web

# 6.4 验证
sleep 5
sudo systemctl status simworld-web --no-pager | head -15
curl -s http://localhost:3002/api/health | head -3
curl -s http://localhost:3002/api/session/status | head -3
REMOTE
```

**前端验证**（从你的浏览器）：

```
https://$DOMAIN/
# 弹出 Basic Auth → 输 alice / CHANGE_ME_alice_pwd
# 加载完成应该看到 SimWorld Studio UI
# 右上方 viewport 会显示 "connecting..." → 10-60s 内出现 UE 画面
```

---

## Phase 7 — 成本控制（可选但推荐）

```bash
# 7.1 装夜间停机（22:00 PT 停，08:00 PT 开）
ssh -i ~/.ssh/id_ed25519 ubuntu@$EC2_IP bash <<'REMOTE'
sudo tee /etc/cron.d/simworld-night-stop <<EOF
# Stop EC2 at 22:00 PT (UTC offset baked in by your aws account region)
0 5 * * * root /usr/bin/aws ec2 stop-instances --instance-ids \$(curl -s http://169.254.169.254/latest/meta-data/instance-id) --region $REGION
EOF
REMOTE

# 7.2 装 IAM role 让实例能 stop 自己
# (在你本地：先创 role + policy + attach)
ROLE_NAME=simworld-self-stop
aws iam create-role --role-name $ROLE_NAME \
  --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' 2>/dev/null || true
aws iam put-role-policy --role-name $ROLE_NAME --policy-name self-stop \
  --policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"ec2:StopInstances","Resource":"*"}]}'
aws iam create-instance-profile --instance-profile-name $ROLE_NAME 2>/dev/null || true
aws iam add-role-to-instance-profile --instance-profile-name $ROLE_NAME --role-name $ROLE_NAME 2>/dev/null || true
aws ec2 associate-iam-instance-profile \
  --instance-id $INSTANCE_ID \
  --iam-instance-profile Name=$ROLE_NAME --region $REGION
```

---

## 出错怎么办

| 症状 | 检查 |
|---|---|
| `bootstrap.sh` 卡在 apt update | EC2 出不了网 → 检查 SG outbound（默认 open，但 corp VPC 会改） |
| `nvidia-smi` 不 work | 没重启；或装错驱动 → `sudo apt install -y nvidia-driver-535 && sudo reboot` |
| UE slot 起不来 | `journalctl -u simworld-web -f` 看 `[slot-N]` 日志；通常是 GPU 显存或 Vulkan |
| Pixel Streaming 黑屏 | 等 60s；再不行查 `/var/lib/simworld/slots/0/logs/cirrus.log` 找 `Streamer connected` |
| WebSocket disconnected | 浏览器拿不到 TURN 凭证 → 检查 `/etc/turnserver.conf` 的 external-ip 和 user |
| Cost 飙升 | 查 `aws ec2 describe-instances` 是不是 stop 失败；查 CloudWatch billing |

## 关键路径速查

| 资源 | 路径 |
|---|---|
| Repo | `/opt/simworld-studio` |
| UE 引擎 | `/opt/ue-engine` |
| UE 项目骨架 | `/opt/simworld-project` |
| Content（HF） | `/opt/simworld-content` |
| 每 slot 运行时 | `/var/lib/simworld/slots/N/` |
| Claude OAuth | `/var/lib/simworld/claude-home/.claude/` |
| Web server 日志 | `journalctl -u simworld-web -f` |
| Slot 日志 | `/var/lib/simworld/slots/N/logs/{ue,cirrus}.log` |
| Nginx 配置 | `/etc/nginx/sites-available/simworld` |
| Auth 文件 | `/etc/nginx/htpasswd` |
| 环境覆盖 | `/etc/default/simworld` |

## 销毁实例

```bash
aws ec2 terminate-instances --instance-ids $INSTANCE_ID --region $REGION
aws ec2 delete-security-group --group-id $SG_ID --region $REGION 2>/dev/null
```
