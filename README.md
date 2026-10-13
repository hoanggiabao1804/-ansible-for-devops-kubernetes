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