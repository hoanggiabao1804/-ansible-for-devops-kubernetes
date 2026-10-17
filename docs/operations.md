# Scaling, Drain/PDB, Idempotency và Integration

Source này tích hợp network-ingress và probes-resources. Các kết quả Azure trong
reliability.md là evidence lịch sử của branch riêng, **không phải kết quả chạy
operations mới**. Chạy acceptance bên dưới trước khi viết kết luận trong report.
Evidence và report nằm ngoài Git source.

## 1. Cấu hình và phạm vi

- `vars/operations.yml`: replicas frontend (mặc định 2), PDB minAvailable=1,
  node đích, phạm vi drain và timeout. Các service khác giữ 1 replica.
- `vars/access.yml`: địa chỉ IP vật lý của Ingress và Host header. Các smoke,
  load và thí nghiệm dùng cùng endpoint. Không cần sửa /etc/hosts của master.
- Chart `0.3.0`: thêm affinity và PDB tùy chọn, giữ probes/resources của 0.2.0.
  PDB thuộc Helm release frontendservice, được gỡ khi uninstall release.
- `site.yml`: cluster → deploy (replicas/PDB/resources/probes) → metrics → ingress
  → verify. Không chứa mua hàng thử, tạo lỗi, drain hay ghi evidence.
- `playbooks/scale.yml` dùng lại deploy/verify. Không dùng kubectl scale rồi để
  lần Helm deploy tiếp theo đưa replicas về giá trị cũ.

Frontend bị giới hạn vào worker inventory và ưu tiên khác node. Anti-affinity
là *preferred*, không phải required: khi drain, cả hai replicas có thể chạy trên
worker sống sót. Hai replicas trên cùng một worker không bảo vệ trước lỗi node.
Scheduler không tự rebalance Pod đang chạy khi thêm node mới; thí nghiệm sẽ từ
chối nếu chưa có frontend Ready ở cả node đích và node khác.

Ingress controller vẫn là ingress-nginx 4.15.1 được kế thừa từ branch đã kiểm
thử, qua NodePort 30080/30443. Không coi controller một replica hoặc các backend
một replica là HA. Kế hoạch chuyển controller sau thời điểm ingress-nginx nghỉ
bảo trì là một công việc riêng; source này giữ controller đã dùng trong evidence.

## 2. Lab hai worker (Vagrant)

Chạy từ thư mục `kubernetes/`. Chuẩn bị dependencies như README. Lab operations
cần tổng RAM VM 10 GiB (master 2 GiB + hai workers 4 GiB), cộng RAM của host.
Vagrant mặc định vẫn chỉ tạo master/node1 để giữ luồng lab nhỏ.

```bash
export PROJECT01_SECOND_WORKER=1
vagrant up
ansible -i inventory-operations.ini k8s -m ping
ansible-playbook -i inventory-operations.ini site.yml
```

Giữ biến môi trường trên khi dùng `vagrant status`, `halt`, `destroy` cho cả lab;
node2 chỉ được khai báo khi biến bằng 1. `inventory-operations.ini` có IP .12
và SSH key Vagrant riêng cho node2. Không dùng inventory này nếu chưa tạo node2.
Azure dùng inventory/extra-vars riêng với hai workers; không hardcode IP Azure
vào source. Hostname node phải trùng tên trong inventory như baseline.

Thêm trên máy browser: `192.168.56.11 boutique.local`; mở
`http://boutique.local:30080/`. Có thể override `storefront_endpoint` nếu dùng
worker khác hoặc SSH tunnel, nhưng Host header phải đúng hostname Ingress.

## 3. Scaling qua Ansible

Trong lab không có tải quan trọng, minh họa 1 → 2 replicas:

```bash
ansible-playbook -i inventory-operations.ini playbooks/scale.yml -e operations_frontend_replicas=1
ansible-playbook -i inventory-operations.ini playbooks/scale.yml
ansible-playbook -i inventory-operations.ini playbooks/smoke.yml
```

Lượt thứ hai dùng cấu hình mặc định 2. Lưu Helm revision, Pod UID/node, replicas
mong đợi/Ready, PDB status và recap từng bước. Verify tự đọc số frontend mong đợi
và kiểm tra mọi node inventory. Với một replica và minAvailable=1, PDB không
cho eviction tự nguyện; đây là hành vi cần giải thích, không phải lỗi của PDB.
PDB không chặn thay đổi replica do scale/Deployment controller.

Nếu chọn một mức scale lâu dài khác, sửa `vars/operations.yml` trong Git; extra
vars chỉ có tác dụng trong lượt chạy đó. Không scale Redis upstream: mỗi replica
có storage riêng, không tự có replication hoặc dữ liệu cart nhất quán.

## 4. Thí nghiệm PDB và drain

Trước khi chạy: kiểm tra `kubectl describe nodes`, tổng requests của tất cả Pod,
capacity worker còn lại, Pod placement và các PDB. Không chỉ cộng requests của
frontend. Requests cho scheduling không phải số đo tài nguyên đang rảnh.

```bash
ansible-playbook -i inventory-operations.ini playbooks/drain-lab.yml \
  -e operations_drain_node=node2 \
  -e operations_drain_delete_emptydir_data=true
```

Đây là **full drain worker**, có thể gián đoạn backend và controller một replica.
Cờ `operations_drain_delete_emptydir_data=true` cho phép mất dữ liệu emptyDir,
bao gồm cart Redis nếu Redis trên node đích; chỉ dùng trong lab có dữ liệu demo.
Mặc định cờ false; preflight chặn full drain khi thấy emptyDir, không tự xóa data.
Không dùng `--force` hoặc `--disable-eviction` để vượt qua PDB.

Luồng experiment:

1. Smoke checkout qua Ingress; yêu cầu hai worker Ready/schedulable và frontend
   phân bố trên target và worker khác. Target không được là control plane hay
   node đã cordon từ trước.
2. Lưu snapshot/PDB; tạm đặt minAvailable bằng số frontend replica.
3. Drain chỉ frontend với timeout 20 giây, phải thấy thông báo PDB từ chối và
   các Pod frontend gốc còn nguyên. Không coi timeout bất kỳ là bằng chứng PDB.
4. Khôi phục PDB gốc, chờ disruption allowance, thực hiện full drain (mặc định)
   với timeout 300 giây. Thu HTTP samples trong quá trình; giữ cả failures.
5. Kiểm tra frontend đủ Ready trên worker khác. Cleanup luôn thử khôi phục PDB
   và uncordon độc lập; sau đó smoke checkout và verify toàn bộ ứng dụng.

Có thể chạy `-e operations_drain_scope=frontend` để chỉ evict frontend và giữ
backend/Redis tại chỗ. **Phải ghi rõ đây là drain có selector**, không tuyên bố
đã evacuate toàn node. Cả hai chế độ vẫn cordon node trong thời gian thí nghiệm.

Evidence trên master trong `/var/tmp/project01-operations/drain-*/` gồm original
PDB, blocked/allowed drain log, snapshots, HTTP samples, cleanup result và smoke
trước/sau. `result.json` của script ghi kết quả drain; playbook còn phải qua smoke
và verify cuối mới được coi là acceptance PASS. HTTP monitor chỉ đo homepage;
không suy diễn mọi giao dịch checkout không gián đoạn từ homepage 200.

`finally` xử lý lỗi và SIGINT/SIGTERM; không thể đảm bảo khi máy mất điện, process
bị SIGKILL hoặc API mất kết nối. Khi bị ngắt, xem original-pdb.json và node state;
chạy deploy để đưa PDB về cấu hình Git, uncordon đúng worker nếu nó do thí nghiệm
cordon, rồi verify/smoke. Không cần rollback Helm vì experiment không đổi chart.
Uncordon không tự rebalance frontend; chạy scale 1 → 2 trong lab nếu cần tái lập
phân bố cho thí nghiệm tiếp theo.

## 5. Checkout/probe experiments sau integration

```bash
ansible-playbook -i inventory-operations.ini playbooks/diagnose-checkout.yml
ansible-playbook -i inventory-operations.ini playbooks/diagnose-probes.yml \
  -e probe_worker_key=/absolute/master/path/to/worker-key \
  -e probe_worker_known_hosts=/absolute/master/path/to/known_hosts
```

Checkout vẫn một replica. Probe playbook phát hiện node thực sự chạy checkout;
SSH key/known_hosts phải dùng được cho node đó từ master. Đây là file trên master,
không phải đường dẫn key Vagrant trên controller host. Không copy private key vào
repo. Nếu các workers có key khác nhau, cung cấp key tương ứng sau khi xem Pod
placement. Thí nghiệm vẫn kiểm tra containerd/Pod UID trước khi gửi SIGSTOP.

Standalone load/measurement dùng IP + Host header:

```bash
sudo python3 scripts/measure-resources.py http://192.168.56.11:30080 \
  --host-header boutique.local --kubeconfig /etc/kubernetes/admin.conf \
  --output-dir /var/tmp/project01-integrated-metrics \
  --idle-seconds 120 --load-seconds 300 --interval 15 --workers 2 --cycle-interval 5
```

Chạy ở máy có kubeconfig và scripts; output-dir phải mới. So sánh số liệu mới
với sizing cũ vì Ingress, replica và topology đã thay đổi. Với nhiều replicas,
summary cộng usage theo Deployment và không đưa gợi ý resource per-container
cho frontend (`provisional_resources: null`); dùng raw CSV để xem từng Pod. Các con số Azure cũ
không tự chứng minh headroom của lab Vagrant mới.

## 6. Idempotency toàn bộ

Chạy site lần đầu để hội tụ (có thể changed), chờ ổn định rồi dùng helper trên
Ansible controller. Không chạy đồng thời load/fault/drain hoặc rollout khác.

```bash
python3 scripts/check-idempotency.py \
  --output-dir ../operations-evidence/idempotency-01 \
  -- -i inventory-operations.ini
```

Helper chạy toàn bộ site hai lần, đòi changed/unreachable/failed/rescued/ignored
đều bằng 0, đồng thời so sánh snapshots trước/sau về Helm revisions, Pod UID,
container IDs/restarts/Ready, Deployment spec, node cordon, Ingress và PDB.
Giữ logs/result/source commit và working-tree diff ngoài source. Dùng commit
sạch cho evidence cuối; snapshot không xuất Secrets/kubeconfig/token.

Apt cache TTL một giờ có thể gây refresh thực tế; ghi rõ task nếu recap khác 0,
không thêm changed_when:false để che. Role Kubernetes được pin vẫn tạo bootstrap
token khi lấy join command với changed_when:false và chỉ báo Flannel `created`.
Do đó helper PASS chứng minh recap và các trạng thái được chọn không đổi, không
chứng minh tuyệt đối không có API writes/tokens mới. Giải thích giới hạn role này
trong báo cáo. Thí nghiệm và smoke tạo giao dịch không thuộc phép thử changed=0.

## 7. Teardown và acceptance cuối

Teardown xóa frontend Ingress trước, uninstall 11 app releases (gồm PDB frontend),
giữ namespace/controller Ingress/Metrics Server và cluster để deploy lại.
Sau teardown, chạy `site.yml` để dựng lại ứng dụng, không chỉ deploy.yml.

Checklist trước khi hoàn thiện report:

- [ ] Clean rebuild bằng môi trường Vagrant đúng inventory và dependency pins.
- [ ] Host đúng 200, host lạ 404, frontend ClusterIP, đủ probes/resources.
- [ ] Checkout qua Ingress, probe recovery và rollback giữ nguyên cấu hình.
- [ ] Scale 1 → 2 bằng Ansible, xác nhận Pod placement.
- [ ] PDB chặn eviction và drain sau đó thành công; cleanup/checkout/verify PASS.
- [ ] Hai lần full-site idempotency PASS và giải thích giới hạn đo.
- [ ] Teardown và redeploy qua site thành công.

Offline source checks:

```bash
ansible-playbook site.yml --syntax-check
ansible-playbook playbooks/drain-lab.yml --syntax-check
python3 -m unittest discover -s tests -v
```

Tham khảo: [kubectl drain](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_drain/),
[PodDisruptionBudget](https://kubernetes.io/docs/tasks/run-application/configure-pdb/).
