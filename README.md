# Project 01 — Part 1–5

Triển khai Kubernetes hai node và Online Boutique bằng Vagrant, Ansible và Helm.
Branch `project01` dùng frontend **NodePort**. Ingress và reliability được phát
triển trên các branch riêng. Report và evidence được lưu ngoài repository.

## Điều kiện chạy

| VM | Private IP | vCPU | RAM | Vai trò |
| --- | --- | --- | --- | --- |
| master | 192.168.56.10 | 2 | 2048 MB | Control plane |
| node1 | 192.168.56.11 | 2 | 4096 MB | Worker chạy 11 thành phần ứng dụng |

Host Linux cần VirtualBox 7+, Vagrant 2.3+, Git, Python 3.11+ và
**ansible-core 2.18+**. Bộ collection đã pin yêu cầu tối thiểu Ansible 2.18;
không dùng Ansible 2.14 với bộ dependency này. Kiểm tra cú pháp khi bổ sung
source dùng ansible-core 2.21.4. Host cần đủ tài nguyên cho tổng 6 GiB RAM của
VM và hệ điều hành host. Host/VM cần Internet để tải dependencies và images.

Chạy **mọi lệnh bên dưới từ thư mục `kubernetes/`**, nơi có `ansible.cfg`:

```bash
cd kubernetes
VBoxManage --version
vagrant --version
ansible --version
git --version
```

Vagrant chỉ tạo VM Ubuntu 22.04; không có provisioner. Ansible dùng
`inventory.ini` và SSH key riêng do Vagrant sinh trong `.vagrant/`.
Không dùng `vagrant provision` để dựng cluster.

## Part 1 — Dependencies và VM

```bash
ansible-galaxy install -r requirements.yml
vagrant up
vagrant status
ansible k8s -m ping
```

`ansible.cfg` đặt roles ở `./roles`, collections ở `./collections`; cả hai được
Git ignore. Không thêm `-p ./roles` vào lệnh cài kết hợp vì Galaxy có thể bỏ qua
collections. Nếu muốn cài riêng:

```bash
ansible-galaxy role install -r requirements.yml -p ./roles
ansible-galaxy collection install -r requirements.yml -p ./collections
```

Pins trong `requirements.yml` lấy theo dependency hiện cài của lab.
`community.general` và `ansible.posix` cung cấp modprobe/sysctl cho role
Kubernetes; `kubernetes.core` cung cấp các module Helm. Nếu hai thư mục local
đã có phiên bản khác, cập nhật theo pins trước khi kiểm thử; có thể thêm
`--force` vào hai lệnh cài riêng để thay dependency local.

Ping phải SUCCESS trên cả master và node1. Key trong inventory chỉ tồn tại
sau khi `vagrant up` thành công. Kiểm tra interface bằng:

```bash
ansible k8s -m ansible.builtin.command -a 'ip -br address'
```

## Part 2 — Kubernetes cluster

```bash
ansible-playbook playbooks/cluster.yml
```

Playbook tắt swap ngay và trong fstab; nạp overlay/br_netfilter; cấu hình
IP forwarding và bridge sysctl; cài containerd dùng systemd cgroups; chạy
kubeadm init/join qua role Kubernetes; cài Helm 3 trên master; đợi từng node
inventory, kube-system pods và Flannel sẵn sàng.

Trong `vars/cluster.yml`:

- `--node-ip={{ ansible_host }}` ép kubelet đăng ký private IP từng VM.
- Flannel dùng `--iface=enp0s8`, interface có IP `192.168.56.x` của lab.
- `enp0s3` thường là NAT; các VM có thể cùng mang IP NAT và không thể dùng
  địa chỉ đó để liên lạc trực tiếp như private network.
- Kubernetes dùng nhánh 1.32; Flannel và Helm được pin phiên bản cụ thể.

Node IP và Flannel interface là hai cấu hình riêng. Node Ready chưa chứng minh
kết nối liên node hoạt động. Nếu tên interface khác, kiểm tra `ip -br address`
rồi chỉnh biến private interface bằng Ansible.

## Part 3 — Online Boutique

```bash
ansible-playbook playbooks/deploy.yml
```

Playbook cài Git, curl, PyYAML và
[helm-diff v3.12.5](https://github.com/databus23/helm-diff/releases/tag/v3.12.5)
trên master dưới user root. Plugin dùng cùng user với các task Helm để so sánh
manifest khi kết hợp `values_files` và inline overrides.

Nguồn: [iankisali/microservice-deployment](https://github.com/iankisali/microservice-deployment).
Clone runtime tại `/opt/microservice-deployment`, pin commit
`8ed96c8d98625c73b67c7172b6703383ea9e46e9`, cùng snapshot được ghi nhận khi
import chart ở branch reliability. Không theo `main` để tránh upstream thay đổi
baseline. Git task quản lý clone; không sửa trực tiếp clone runtime.

Hai chart `chart/microservice` và `chart/redis` tạo 11 Helm releases trong
namespace `microservice`. Các overrides nằm trong `vars/microservices.yml`:

| Override | Phạm vi | Mục đích |
| --- | --- | --- |
| `appReplicas: 1` | Mọi release | Giảm nhu cầu tài nguyên của lab |
| `serviceType: NodePort` | Frontend | Truy cập từ host, không cần cloud LoadBalancer |
| `appName: checkoutservice` | Checkout | Sửa tên Deployment/Service mà frontend gọi |

Upstream checkout values có hai key `appImage`, thiếu `appName`, khiến chart
rơi về tên `servicename`. Playbook render upstream để xác nhận lỗi trước khi
áp dụng override. Frontend cần `checkoutservice:5050`; homepage vẫn có thể
hoạt động dù checkout lỗi. Khi đổi upstream SHA, kiểm tra lại assertion tái
hiện bug và các overrides.

Cuối playbook in URL `http://192.168.56.11:<NodePort>/`. Dùng port trong output,
không hardcode port của lần deploy trước.

## Part 4 — Verify và thử ứng dụng

```bash
ansible-playbook playbooks/verify.yml
```

Playbook assert hai node Ready và đúng private IP, 11 deployments có một
replica Ready/Available, checkout có tên đúng và không còn `servicename`,
frontend là NodePort hợp lệ, HTTP 200 và nội dung chứa một marker trong
`vars/verify.yml`. HTTP request chạy từ master tới worker NodePort.

Mở URL trên browser của host, xem sản phẩm, thêm hàng vào cart, reload kiểm tra
cart còn dữ liệu và hoàn tất một đơn hàng. Homepage HTTP 200 không thay thế
kiểm thử checkout. Lưu kết quả ở báo cáo bên ngoài repo.

Có thể chạy Parts 2–4 bằng entry point chung sau khi tạo VM:

```bash
ansible-playbook site.yml
```
