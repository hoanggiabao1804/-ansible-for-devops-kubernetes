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

## Checkout Bug Diagnosis — nhiệm vụ đầu của Tuân

Sau khi baseline hoạt động, chạy từ thư mục repo với inventory mặc định:

```bash
ansible-playbook playbooks/diagnose-checkout.yml
```

Playbook riêng này không nằm trong `site.yml`. Nó lưu trạng thái ban đầu,
render chart upstream, bỏ override `appName` của riêng checkout bằng Ansible
Helm để tái hiện tên sai `servicename`, kiểm tra checkout lỗi rồi khôi phục và
chạy verification. **Checkout tạm thời không hoạt động trong thí nghiệm.**
Các lần kiểm thử dùng phiên cart mới và dữ liệu demo có sẵn trong form.

Evidence nằm ngoài repo, trên master tại
`/tmp/project01-checkout-diagnosis/<timestamp>/`. Có thể đổi thư mục và giữ
trạng thái lỗi thêm 60 giây để chụp màn hình:

```bash
ansible-playbook playbooks/diagnose-checkout.yml \
  -e checkout_diagnosis_root=/var/tmp/project01-checkout-evidence \
  -e checkout_diagnosis_observation_seconds=60
```

Cửa sổ quan sát mặc định là `0`, cho phép từ `0` đến `300` giây. Khi task
thí nghiệm lỗi, block `always` cố khôi phục release bằng values đã lưu.
Probe sau khôi phục thử lại tối đa 30 lần, cách nhau 10 giây, để chờ frontend
kết nối lại qua gRPC; mỗi lần thử vẫn lưu evidence riêng.
Nếu tiến trình Ansible bị ngắt, kill hoặc master mất kết nối, phần khôi phục có thể chưa chạy;
đưa lab về baseline bằng:

```bash
ansible-playbook playbooks/deploy.yml
ansible-playbook playbooks/verify.yml
```

Có thể kiểm thử một đơn hàng demo riêng; thay `<NodePort>` bằng port thực tế
và dùng private IP của lab hoặc URL loopback qua SSH tunnel:

```bash
python3 scripts/checkout-smoke.py 'http://192.168.56.11:<NodePort>' \
  --expect success --evidence-dir ../project01-evidence/checkout-smoke
```

Probe lưu HTML và JSON cho mỗi lần thử trong thư mục `attempt-*`, giữ cả
lần lỗi khi retry. `--expect failure` chỉ PASS khi checkout trả HTTP 500 với
lỗi kết nối liên quan `checkoutservice`, trong khi browse/cart vẫn hoạt động
và cart còn hàng. Báo cáo incident và screenshot cũng lưu ngoài repo; ghi rõ
đây là tái hiện có kiểm soát của bug đã được mô tả và sửa trong baseline.

## Kiểm tra source và idempotency

Sau khi cài dependency, kiểm tra local không cấu hình VM:

```bash
ansible-inventory --graph
ansible-playbook site.yml --syntax-check
ansible-playbook playbooks/teardown.yml --syntax-check
```

Sau lần triển khai thành công, chạy lại `ansible-playbook site.yml` hai lần
khi không đổi cấu hình. Mục tiêu recap là `changed=0` khi trạng thái đã hội tụ.
So sánh Helm revisions trước/sau, tránh upgrade lại chỉ vì values overrides:

```bash
ansible k8s_master -b -m ansible.builtin.command -a '/usr/local/bin/helm list -n microservice --kubeconfig /etc/kubernetes/admin.conf'
```

Nếu còn changed, đọc tên task và nguyên nhân; không che mutation bằng
`changed_when: false`. Apt có cache TTL một giờ nên refresh sau TTL cần được
phân biệt với Helm upgrade không cần thiết.

Giới hạn của role Kubernetes 8.2.0: task lấy join command tạo bootstrap token
mỗi lần chạy nhưng dùng `changed_when: false`; task apply Flannel chỉ tính
`created`, không tính `configured`. Recap không chứng minh tuyệt đối không có
mutation. Ghi nhận giới hạn này khi giải thích idempotency; không sửa tay role
tải về rồi coi đó là source đã quản lý trong Git.

`--check` không thay thế clean build: nhiều task phụ thuộc VM, binary, clone
và cluster đã tồn tại. Chuyển Git branch cũng không tự đưa cluster từ
Ingress/reliability về trạng thái baseline.

## Part 5 — Teardown

Gỡ app và giữ cluster:

```bash
ansible-playbook playbooks/teardown.yml
```

Playbook uninstall 11 Helm releases theo thứ tự đảo, assert không còn release
và application deployment, kiểm tra từng node, kube-system và Flannel.
Namespace `microservice` và clone upstream được giữ để deploy lần sau.

Dừng VM, giữ disk:

```bash
vagrant halt
```

Xóa VM và disk để chủ động dựng lại từ đầu:

```bash
vagrant destroy -f
```

Destroy xóa toàn bộ cluster trong VM. Chỉ dùng khi đã sẵn sàng mất trạng thái
hiện tại; sau đó lặp lại Parts 1–4 để kiểm thử clean build. Không tạo lab trùng
tên VM/private IP khi lab hiện tại vẫn đang chạy.

## Cấu trúc và ví dụ legacy

| Đường dẫn | Vai trò |
| --- | --- |
| `Vagrantfile`, `inventory.ini`, `ansible.cfg` | VM và kết nối Ansible |
| `requirements.yml` | Phiên bản roles/collections |
| `site.yml` | Build → deploy → verify |
| `playbooks/{cluster,deploy,verify,teardown}.yml` | Luồng Parts 2–5 |
| `vars/{cluster,microservices,verify}.yml` | Cấu hình project |

`main.yml`, `vars/main.yml`, `inventory`, `examples/` và manifest cũ trong
`files/` là ví dụ gốc từ sách, **không thuộc luồng Parts 1–5**. Chúng dùng IP,
role hoặc phiên bản khác. Dùng entry points trong README này. Manifest Flannel
thực tế được download và patch bởi `playbooks/cluster.yml`.

`.vagrant/`, dependencies và kubeconfig được Git ignore. Không commit SSH
private key, kubeconfig, join token; tránh đưa token từ verbose logs vào báo cáo.

Dự án kế thừa ví dụ [Ansible for DevOps](https://github.com/geerlingguy/ansible-for-devops)
của Jeff Geerling; giữ thông tin bản quyền và LICENSE gốc.
