variable "region" {
  type    = string
  default = "us-east-1"
}

variable "availability_zone" {
  type        = string
  default     = null
  description = "Pin an AZ. If apply fails with InsufficientInstanceCapacity, set another one (e.g. us-east-1b)."
}

variable "instance_type" {
  type        = string
  default     = "c7i.metal-24xl"
  description = "Bare metal by default. Any KVM-capable x86_64 type works (virtual c7i/c8i with nested virtualization needs nested_virtualization=true and results are flagged nested)."
}

variable "nested_virtualization" {
  type    = bool
  default = false
}

variable "public_key_path" {
  type    = string
  default = "~/.ssh/id_ed25519.pub"
}

variable "ssh_cidr" {
  type        = string
  default     = null
  description = "CIDR allowed to SSH in. Default: your current public IP."
}

variable "ttl_hours" {
  type        = number
  default     = 8
  description = "The instance shuts itself down (and terminates) after this many hours."
}

variable "volume_gb" {
  type    = number
  default = 200
}

variable "ami_ssm_parameter" {
  type    = string
  default = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}
