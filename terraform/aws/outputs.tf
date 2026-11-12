output "public_ip" {
  value = aws_instance.bench.public_ip
}

output "inventory" {
  description = "Ansible inventory: terraform -chdir=terraform/aws output -raw inventory > hosts"
  value       = <<-EOT
    [bench_host]
    bench ansible_host=${aws_instance.bench.public_ip} ansible_user=ubuntu ansible_ssh_private_key_file=${trimsuffix(pathexpand(var.public_key_path), ".pub")}

    [vms]
    dut
    loadgen
    backend
  EOT
}
