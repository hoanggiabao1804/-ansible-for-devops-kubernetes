# -*- mode: ruby -*-
# vi: set ft=ruby :

Vagrant.configure("2") do |config|
  # Ubuntu 22.04 LTS as required by Project 1.
  config.vm.box = "ubuntu/jammy64"

  # Let Vagrant generate a private SSH key for each machine.
  config.ssh.insert_key = true

  # Do not depend on the /vagrant synced directory.
  # Vagrant is used only to create the virtual machines.
  config.vm.synced_folder ".", "/vagrant", disabled: true

  config.vm.boot_timeout = 600

  machines = [
    {
      name: "master",
      ip: "192.168.56.10",
      cpus: 2,
      memory: 2048
    },
    {
      name: "node1",
      ip: "192.168.56.11",
      cpus: 2,
      memory: 4096
    }
  ]

  machines.each do |machine|
    config.vm.define machine[:name] do |node|
      node.vm.hostname = machine[:name]

      node.vm.network "private_network",
        ip: machine[:ip]

      node.vm.provider "virtualbox" do |vb|
        vb.name = "project01-#{machine[:name]}"
        vb.cpus = machine[:cpus]
        vb.memory = machine[:memory]
      end
    end
  end
end