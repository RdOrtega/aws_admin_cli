.PHONY: install up down logs seed test test-e2e cov lint fmt typecheck check clean doctor tui demo-iam demo-s3 demo-vpc demo-ec2 demo-stack

install:
	poetry install

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

# Re-runs the network-seeding bootstrap (localstack/init/01-bootstrap.sh) inside the
# already-running container, without restarting LocalStack. Idempotent -- safe to run
# repeatedly. `make up` already runs it once on first boot; this is for re-seeding
# after `docker compose down -v` wiped the LocalStack state, or after editing the script.
# Resolves the container via `docker compose ps -q` rather than a hardcoded name --
# the actual container name depends on COMPOSE_PROJECT_NAME/the compose file's own
# `container_name`, which has bitten us before (a same-named container from a
# different checkout of this repo). Fails with a clear message instead of a raw
# "No such container" if LocalStack isn't up at all.
seed:
	@cid=$$(docker compose ps -q localstack); \
	 test -n "$$cid" || { echo "LocalStack no está corriendo. Ejecuta 'make up'."; exit 1; }; \
	 docker exec $$cid bash /etc/localstack/init/ready.d/01-bootstrap.sh

test:
	poetry run pytest -m "not e2e"

test-e2e:
	# --no-cov: an e2e-only subset can never satisfy the whole-repo coverage gate
	# (fail_under=80 in pyproject.toml); that gate is enforced by `test`/`cov`/`check`.
	poetry run pytest -m "e2e" --no-cov

doctor:
	poetry run aws-admin-cli doctor

# Launches the interactive TUI (arrow-key menus over the same use cases every
# command above calls). To target LocalStack instead of your default profile,
# run `poetry run aws-admin-cli --profile localstack --endpoint-url http://localhost:4566
# --interactive` directly -- global options come before --interactive, same as any
# other command.
tui:
	poetry run aws-admin-cli --interactive

# Requires `make up` and the `localstack` profile from docs/aws-profiles.md.
# Runs a real create -> attach -> list -> detach -> delete IAM sequence against
# LocalStack: a rehearsal for the EC2 instance-profile flow coming in Fase 5.
demo-iam:
	$(eval CLI := poetry run aws-admin-cli --profile localstack --endpoint-url http://localhost:4566)
	$(eval POLICY_ARN := arn:aws:iam::000000000000:policy/demo-read-only)
	$(CLI) iam role create demo-ec2-role --service ec2.amazonaws.com
	$(CLI) iam policy create demo-read-only --document-json '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":["s3:GetObject","s3:ListBucket"],"Resource":["arn:aws:s3:::demo-bucket","arn:aws:s3:::demo-bucket/*"]}]}'
	$(CLI) iam role attach-policy demo-ec2-role --policy-arn $(POLICY_ARN)
	$(CLI) iam role policies demo-ec2-role
	$(CLI) iam role detach-policy demo-ec2-role --policy-arn $(POLICY_ARN)
	$(CLI) iam policy delete $(POLICY_ARN) --yes
	$(CLI) iam role delete demo-ec2-role --yes

# Requires `make up` and the `localstack` profile from docs/aws-profiles.md.
# Runs a real create -> upload -> list -> download -> presign -> empty -> delete
# S3 sequence against LocalStack.
demo-s3:
	$(eval CLI := poetry run aws-admin-cli --profile localstack --endpoint-url http://localhost:4566)
	$(eval BUCKET := demo-s3-bucket)
	$(eval FILE := /tmp/aws-admin-cli-demo-s3.txt)
	echo "hello from aws-admin-cli demo-s3" > $(FILE)
	$(CLI) s3 bucket create $(BUCKET) --if-not-exists
	$(CLI) s3 cp $(FILE) s3://$(BUCKET)/demo.txt
	$(CLI) s3 ls s3://$(BUCKET)
	$(CLI) s3 cp s3://$(BUCKET)/demo.txt /tmp/aws-admin-cli-demo-s3-downloaded.txt
	$(CLI) s3 presign s3://$(BUCKET)/demo.txt --expires-in 900
	$(CLI) s3 bucket delete $(BUCKET) --force --yes
	rm -f $(FILE) /tmp/aws-admin-cli-demo-s3-downloaded.txt

# Requires `make up` (or `make seed`) and the `localstack` profile from
# docs/aws-profiles.md. Read-only walkthrough of the network seeded by
# localstack/init/01-bootstrap.sh: no create/modify/delete calls -- the vpc
# module never issues any (see docs/least-privilege.md).
demo-vpc:
	$(eval CLI := poetry run aws-admin-cli --profile localstack --endpoint-url http://localhost:4566)
	$(CLI) vpc list
	$(CLI) vpc subnet list --public
	$(CLI) vpc subnet list --private
	$(CLI) vpc sg list --vpc corp-main-vpc
	$(CLI) vpc sg audit --vpc corp-main-vpc
	$(CLI) vpc resolve --vpc corp-main-vpc --subnet corp-private-1a --sg corp-web-sg

# Requires `make up` (and `make seed` for the corp-private-1a/corp-web-sg network)
# and the `localstack` profile from docs/aws-profiles.md. Runs the full EC2 flow: an
# IAM role -> launch (--iam-role wires the instance profile) -> show -> stop -> start
# -> terminate, against LocalStack's fictitious AMI catalog. The subnet/SG themselves
# are never created here -- see docs/least-privilege.md#separation-of-duties.
demo-ec2:
	$(eval CLI := poetry run aws-admin-cli --profile localstack --endpoint-url http://localhost:4566)
	$(eval ROLE := demo-ec2-role)
	$(eval AMI := $(shell $(CLI) --output json ec2 ami list | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['ImageId'])"))
	$(CLI) iam role create $(ROLE) --service ec2.amazonaws.com
	$(CLI) ec2 instance launch demo-web --ami $(AMI) --type t3.micro \
		--subnet corp-private-1a --sg corp-web-sg --iam-role $(ROLE) --wait --yes
	$(CLI) ec2 instance show demo-web
	$(CLI) ec2 instance stop demo-web --wait --yes
	$(CLI) ec2 instance start demo-web --wait --yes
	$(CLI) ec2 instance terminate demo-web --wait --yes
	$(CLI) iam role delete $(ROLE) --yes

# Requires `make up` (and `make seed`) and the `localstack` profile from
# docs/aws-profiles.md. Runs the full stack flow: apply examples/webapp-stack.yaml
# (bucket + role + policy + attachment + instance, in dependency order) -> status
# --refresh (drift check) -> destroy. Uses --output json throughout to demonstrate
# a scriptable flow; `demo-webapp` is the manifest's own `name:`, not a Makefile var.
demo-stack:
	$(eval CLI := poetry run aws-admin-cli --profile localstack --endpoint-url http://localhost:4566)
	$(CLI) stack validate examples/webapp-stack.yaml
	$(CLI) stack plan examples/webapp-stack.yaml
	$(CLI) stack apply examples/webapp-stack.yaml --yes
	$(CLI) --output json stack status demo-webapp --refresh
	$(CLI) stack destroy demo-webapp --yes

cov:
	poetry run pytest -m "not e2e" --cov-report=html

lint:
	poetry run ruff check .

fmt:
	poetry run ruff format .

typecheck:
	poetry run mypy src

check: lint typecheck test

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	find . -type d -name "__pycache__" -not -path "./.venv/*" -exec rm -rf {} +
