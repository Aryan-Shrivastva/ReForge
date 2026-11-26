FROM node:22-alpine AS build
WORKDIR /dashboard
ARG VITE_PUBLIC_REPOSITORIES_ONLY=false
ENV VITE_PUBLIC_REPOSITORIES_ONLY=$VITE_PUBLIC_REPOSITORIES_ONLY
COPY package*.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /dashboard/dist /usr/share/nginx/html
EXPOSE 80
