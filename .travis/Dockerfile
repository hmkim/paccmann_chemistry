FROM drugilsberg/rdkit-ubuntu:latest
RUN apt-get update && apt-get install -y git
WORKDIR /chemistry
# install requirements
COPY examples/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
# copy paccmann_chemistry
COPY . .
RUN pip3 install --no-deps .
CMD /bin/bash